import SwiftUI
import ARKit
import RealityKit
import UIKit

final class RoomAnchoringController: NSObject, ObservableObject, ARSessionDelegate {
    @Published private(set) var trackingQuality: TrackingQuality = .initializing
    @Published private(set) var roomWorldTransform: simd_float4x4?
    @Published private(set) var isReady = false
    @Published private(set) var isNavigationActive = false
    @Published private(set) var destinationNames: [String] = []
    @Published private(set) var activeStartName: String?
    @Published private(set) var activeDestinationName: String?
    @Published private(set) var facingSurfaceName = "--"
    @Published var statusMessage = "Stand at the saved starting point and face into the room."
    @Published var currentInstruction = "Align the saved plan to the live camera feed."
    @Published var distanceRemainingText = "--"
    @Published var obstacleMessage: String?

    // AR Odometry Tracker
    let trackerState = ARTrackerState()
    private lazy var trackerProcessor = ARTrackerProcessor(state: trackerState)

    // Conversation messages for the assistant UI
    @Published var conversationMessages: [ConversationMessage] = []

    private let envelope: RoomModelEnvelope
    private let visualMeshURL: URL?
    private weak var arView: ARView?
    private var roomAnchor: AnchorEntity?
    private var guideAnchor: AnchorEntity?
    private var destinationResolver: RoomDestinationResolver?
    private var navigator: RoomNavigator?
    private let obstacleDetector = BackendVisionClassifier()
    private let speechEngine = NavigationSpeechEngine.shared
    private var lastNavigationUpdateDate = Date.distantPast
    private var lastPathRenderDate = Date.distantPast
    private var destinationWorldPoint: SIMD3<Float>?
    private var activePathOrigin: SIMD3<Float>?
    @Published private(set) var remainingWaypoints: [SIMD3<Float>] = []
    @Published private(set) var renderedPath: [SIMD3<Float>] = []
    @Published private(set) var currentCameraTransform: simd_float4x4?
    private var lastTurnAnnouncementKey: String?
    private var lastGuidanceSpeechDate = Date.distantPast
    private var offPathBeganAt: Date?
    private var lastObstacleSampleDate = Date.distantPast
    private var lastObstacleLabel: String?
    private var lastObstacleAlertDate = Date.distantPast
    private var lastRecalculationDate = Date.distantPast
    private var hasStartedSession = false

    init(envelope: RoomModelEnvelope, visualMeshURL: URL? = nil) {
        self.envelope = envelope
        self.visualMeshURL = visualMeshURL
        super.init()
    }

    /// Plain-text summary of the room for LLM context.
    var roomContextSummary: String {
        let snap = envelope.capturedRoomSnapshot
        let bounds = snap.roomBounds
        var lines: [String] = []
        lines.append(String(format: "Room dimensions: %.1fm wide × %.1fm deep × %.1fm tall.",
                            bounds.widthMeters, bounds.depthMeters, bounds.heightMeters))
        lines.append("Walls: \(snap.walls.count). Doors: \(snap.doors.count). Windows: \(snap.windows.count). Objects: \(snap.objects.count).")

        for door in snap.doors {
            lines.append("- Door: \(RoomLabeling.displayName(for: door)) (width: \(String(format: "%.1fm", door.dimensionsMeters.x)))")
        }
        for obj in snap.objects {
            lines.append("- Object: \(RoomLabeling.displayName(for: obj)) (\(obj.category), \(String(format: "%.1fm × %.1fm", obj.dimensionsMeters.x, obj.dimensionsMeters.z)))")
        }

        if let anchor = envelope.entryAnchor.anchorType {
            lines.append("Entry point: \(anchor) #\((envelope.entryAnchor.anchorIndex ?? 0) + 1).")
        }
        return lines.joined(separator: "\n")
    }

    var roomContextJSON: String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(envelope),
              let json = String(data: data, encoding: .utf8) else {
            return ""
        }
        return json
    }

    func attach(arView: ARView) {
        self.arView = arView
        arView.session.delegate = self
    }

    func startSession() {
        guard !hasStartedSession, let arView else { return }

        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravityAndHeading // Enables compass heading
        arView.session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
        obstacleDetector.prepareModelIfNeeded()
        hasStartedSession = true
    }

    func stopSession() {
        speechEngine.stopSpeaking()
        arView?.session.delegate = nil
        arView?.session.pause()
        arView?.scene.anchors.removeAll()
        roomAnchor = nil
        guideAnchor = nil
        arView = nil
        hasStartedSession = false
    }

    func alignUsingCurrentCameraPose() {
        guard let frame = arView?.session.currentFrame else {
            statusMessage = "ARKit has not produced a camera frame yet."
            return
        }

        guard case .ready = TrackingGate.decision(for: trackingQuality) else {
            statusMessage = "Hold still until tracking quality is normal, then try again."
            return
        }

        let roomTransform = RoomAnchorMath.roomWorldTransform(
            qrAnchorWorld: frame.camera.transform,
            entryAnchorRoom: envelope.entryAnchor.transformMatrix.simd
        )

        roomWorldTransform = roomTransform
        currentCameraTransform = frame.camera.transform
        isReady = true
        statusMessage = "Room aligned to the live camera feed."
        currentInstruction = "Tell me where you'd like to go."

        // Reset odometry tracker for this navigation session
        trackerState.reset()
        trackerProcessor.resetPositionTracking()
        distanceRemainingText = "--"
        obstacleMessage = nil
        activePathOrigin = nil
        offPathBeganAt = nil
        lastObstacleSampleDate = .distantPast
        lastObstacleLabel = nil
        lastGuidanceSpeechDate = .distantPast

        destinationResolver = RoomDestinationResolver(envelope: envelope, roomWorldTransform: roomTransform)
        navigator = RoomNavigator(snapshot: envelope.capturedRoomSnapshot, roomWorldTransform: roomTransform)
        destinationNames = destinationResolver?.destinationNames ?? []
        facingSurfaceName = facingSurfaceName(for: frame.camera.transform, roomWorldTransform: roomTransform)

        renderRoom(at: roomTransform)
        stopNavigation(resetInstruction: false)
    }

    @discardableResult
    func startNavigation(from sourceName: String? = nil, to destinationName: String) -> NavigationRequestResult {
        guard
            isReady,
            let camTransform = currentCameraTransform,
            let resolver = destinationResolver,
            let navigator
        else {
            return NavigationRequestResult(
                didStart: false,
                response: "Align the saved room model before starting navigation."
            )
        }

        // Use the cached camera transform — never access session.currentFrame
        // which retains an ARFrame and causes the "retaining 11 ARFrames" flood.
        let userPosition = RoomGeometry.translation(of: camTransform)
        resolver.updateUserWorldPosition(userPosition)

        guard let candidate = resolver.resolveCandidate(destinationName) else {
            currentInstruction = "Could not find that destination in the saved room model."
            return NavigationRequestResult(
                didStart: false,
                response: "I could not find \(destinationName) in this saved room."
            )
        }

        // Check if user is already at the destination
        let distanceToDest = NavigationGuidanceMath.planarDistance(userPosition, candidate.worldPosition)
        if distanceToDest < 0.8 {
            currentInstruction = "You are already at \(candidate.name)."
            return NavigationRequestResult(
                didStart: false,
                response: "You're already at \(candidate.name)! Is there somewhere else you'd like to go?"
            )
        }

        activeStartName = "Current Location"

        if let sourceName, !sourceName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            guard let sourceCandidate = resolver.resolveCandidate(sourceName) else {
                currentInstruction = "Could not find \(sourceName) in the saved room model."
                return NavigationRequestResult(
                    didStart: false,
                    response: "I could not find \(sourceName) in this saved room."
                )
            }

            let sourceDistance = NavigationGuidanceMath.planarDistance(userPosition, sourceCandidate.worldPosition)
            if sourceDistance > 1.4 {
                activeStartName = sourceCandidate.name
                currentInstruction = "Move to \(sourceCandidate.name) to begin this route."
                distanceRemainingText = "--"
                return NavigationRequestResult(
                    didStart: false,
                    response: String(format: "You're %.1f meters away from %@. Move there first, then I'll guide you to %@.", sourceDistance, sourceCandidate.name, candidate.name)
                )
            }

            activeStartName = sourceCandidate.name
        }

        let path = navigator.findPath(from: userPosition, to: candidate.worldPosition)
        guard !path.isEmpty else {
            currentInstruction = "Could not map a walkable route to \(candidate.name)."
            return NavigationRequestResult(
                didStart: false,
                response: "I could not map a walkable route to \(candidate.name)."
            )
        }

        activeDestinationName = candidate.name
        destinationWorldPoint = candidate.worldPosition
        activePathOrigin = userPosition
        remainingWaypoints = path
        renderedPath = pathWithDestination(path, destination: candidate.worldPosition)
        isNavigationActive = true
        lastTurnAnnouncementKey = nil
        obstacleMessage = nil
        offPathBeganAt = nil
        lastObstacleSampleDate = .distantPast
        lastObstacleLabel = nil
        lastGuidanceSpeechDate = Date()

        let remainingDistance = NavigationGuidanceMath.distanceRemaining(
            currentWorldPoint: userPosition,
            remainingWaypoints: renderedPath
        )
        let nextWaypoint = remainingWaypoints.first ?? candidate.worldPosition
        let distanceToNext = NavigationGuidanceMath.planarDistance(userPosition, nextWaypoint)
        let turnAngle = NavigationGuidanceMath.turnAngleDegrees(
            currentHeadingTransform: camTransform,
            targetWorldPoint: nextWaypoint,
            currentWorldPoint: userPosition
        )

        currentInstruction = guidanceInstruction(
            turnAngle: turnAngle,
            distanceToNext: distanceToNext,
            distanceRemaining: remainingDistance,
            destinationName: candidate.name
        )
        distanceRemainingText = formattedDistance(remainingDistance)
        lastTurnAnnouncementKey = guidanceAnnouncementKey(
            turnAngle: turnAngle,
            distanceRemaining: remainingDistance,
            destinationName: candidate.name
        )

        renderPath()
        return NavigationRequestResult(didStart: true, response: currentInstruction)
    }

    func stopNavigation(resetInstruction: Bool = true) {
        speechEngine.stopSpeaking()
        isNavigationActive = false
        activeStartName = nil
        activeDestinationName = nil
        destinationWorldPoint = nil
        activePathOrigin = nil
        remainingWaypoints = []
        renderedPath = []
        distanceRemainingText = "--"
        lastTurnAnnouncementKey = nil
        lastGuidanceSpeechDate = .distantPast
        offPathBeganAt = nil
        lastObstacleSampleDate = .distantPast
        lastObstacleLabel = nil
        obstacleMessage = nil

        if let guideAnchor, let arView {
            arView.scene.anchors.remove(guideAnchor)
            self.guideAnchor = nil
        }

        if resetInstruction {
            currentInstruction = isReady ? "Choose a destination to start navigation." : "Align the saved plan to the live camera feed."
        }
    }

    // MARK: - ARSession Delegate (Frame-Free Design)
    //
    // CRITICAL: Extract all needed values from ARFrame synchronously on
    // the ARKit callback thread. The ARFrame is released when this returns.

    /// Lightweight value type — no ARFrame-retaining references.
    private struct FrameSnapshot {
        let cameraTransform: simd_float4x4
        let trackingState: ARCamera.TrackingState
        let eulerAnglesY: Float
    }



    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let shouldAnalyzeObstacle = isNavigationActive && Date().timeIntervalSince(lastObstacleSampleDate) >= 1.0
        if shouldAnalyzeObstacle {
            lastObstacleSampleDate = Date()
            if let frameInput = obstacleDetector.makeFrameInput(from: frame.capturedImage) {
                obstacleDetector.detectFrontObstacle(from: frameInput) { [weak self] warning in
                    self?.applyFrontObstacleWarning(warning)
                }
            }
        }

        // Extract ONLY lightweight scalar values — NO ARMeshAnchor, NO CVPixelBuffer.
        // This guarantees the ARFrame is released when this callback returns.
        let snapshot = FrameSnapshot(
            cameraTransform: frame.camera.transform,
            trackingState: frame.camera.trackingState,
            eulerAnglesY: frame.camera.eulerAngles.y
        )

        // Run obstacle detection SYNCHRONOUSLY on this callback thread while the
        // ARFrame is still on the stack. This way mesh anchors are never captured
        // by a closure and never retain the ARFrame.

        // ARFrame + meshAnchors + pixelBuffer are ALL released here.

        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.trackerProcessor.processTransform(
                transform: snapshot.cameraTransform,
                trackingState: snapshot.trackingState,
                eulerAnglesY: snapshot.eulerAnglesY
            )
            self.handleFrameSnapshot(snapshot)
        }
    }

    @MainActor
    private func handleFrameSnapshot(_ snapshot: FrameSnapshot) {
        trackingQuality = TrackingQuality(trackingState: snapshot.trackingState)
        currentCameraTransform = snapshot.cameraTransform

        if !isReady {
            switch TrackingGate.decision(for: trackingQuality) {
            case .ready:
                statusMessage = "Tracking looks good. Stand at the entry and tap Align Here."
            case .holdStill(let message):
                statusMessage = message
            }
            return
        }

        guard case .ready = TrackingGate.decision(for: trackingQuality) else {
            statusMessage = "Hold still until tracking quality is normal."
            return
        }

        let currentPosition = SIMD3<Float>(
            snapshot.cameraTransform.columns.3.x,
            snapshot.cameraTransform.columns.3.y,
            snapshot.cameraTransform.columns.3.z
        )
        destinationResolver?.updateUserWorldPosition(currentPosition)
        statusMessage = "Room aligned to the live camera feed."
        if let roomWorldTransform {
            facingSurfaceName = facingSurfaceName(for: snapshot.cameraTransform, roomWorldTransform: roomWorldTransform)
        }

        if isNavigationActive {
            let now = Date()
            if now.timeIntervalSince(lastNavigationUpdateDate) >= 0.1 {
                lastNavigationUpdateDate = now
                advanceNavigation(snapshot: snapshot, currentPosition: currentPosition)
            }
        }
    }

    @MainActor
    private func advanceNavigation(snapshot: FrameSnapshot, currentPosition: SIMD3<Float>) {
        guard
            let destinationWorldPoint,
            let activeDestinationName,
            let navigator
        else {
            stopNavigation()
            return
        }

        while let first = remainingWaypoints.first,
              NavigationGuidanceMath.shouldAdvanceWaypoint(currentWorldPoint: currentPosition, waypoint: first) {
            activePathOrigin = first
            remainingWaypoints.removeFirst()
        }

        if NavigationGuidanceMath.hasArrived(currentWorldPoint: currentPosition, destinationWorldPoint: destinationWorldPoint) {
            currentInstruction = "You have arrived at \(activeDestinationName)."
            distanceRemainingText = "0.0 m"
            renderPath(clearOnly: true)
            isNavigationActive = false
            speechEngine.speak("You have arrived at \(activeDestinationName).")
            lastGuidanceSpeechDate = Date()
            return
        }

        let pathForChecks = guidancePathForChecks(currentPosition: currentPosition, destination: destinationWorldPoint)
        if NavigationGuidanceMath.isOffPath(currentWorldPoint: currentPosition, waypoints: pathForChecks) {
            if offPathBeganAt == nil {
                offPathBeganAt = Date()
            }

            let hasBeenOffPathLongEnough = Date().timeIntervalSince(offPathBeganAt ?? Date()) > 1.25
            if hasBeenOffPathLongEnough, Date().timeIntervalSince(lastRecalculationDate) > 2.5 {
                lastRecalculationDate = Date()
                let updatedPath = navigator.findPath(from: currentPosition, to: destinationWorldPoint)
                guard !updatedPath.isEmpty else { return }
                speechEngine.speak("Recalculating.")
                lastGuidanceSpeechDate = Date()
                currentInstruction = "Recalculating."
                activePathOrigin = currentPosition
                remainingWaypoints = updatedPath
                offPathBeganAt = nil
            }
        } else {
            offPathBeganAt = nil
        }

        renderedPath = pathWithDestination(remainingWaypoints, destination: destinationWorldPoint)
        let now = Date()
        if now.timeIntervalSince(lastPathRenderDate) >= 0.5 {
            lastPathRenderDate = now
            renderPath()
        }

        let nextWaypoint = remainingWaypoints.first ?? destinationWorldPoint
        let distanceToNext = NavigationGuidanceMath.planarDistance(currentPosition, nextWaypoint)
        let turnAngle = NavigationGuidanceMath.turnAngleDegrees(
            currentHeadingTransform: snapshot.cameraTransform,
            targetWorldPoint: nextWaypoint,
            currentWorldPoint: currentPosition
        )
        let remainingDistance = NavigationGuidanceMath.distanceRemaining(
            currentWorldPoint: currentPosition,
            remainingWaypoints: renderedPath
        )

        currentInstruction = guidanceInstruction(
            turnAngle: turnAngle,
            distanceToNext: distanceToNext,
            distanceRemaining: remainingDistance,
            destinationName: activeDestinationName
        )
        let announcementKey = guidanceAnnouncementKey(
            turnAngle: turnAngle,
            distanceRemaining: remainingDistance,
            destinationName: activeDestinationName
        )
        if lastTurnAnnouncementKey != announcementKey || now.timeIntervalSince(lastGuidanceSpeechDate) >= 4.0 {
            speechEngine.speak(currentInstruction)
            lastTurnAnnouncementKey = announcementKey
            lastGuidanceSpeechDate = now
        }

        distanceRemainingText = formattedDistance(remainingDistance)
        if obstacleMessage != nil, Date().timeIntervalSince(lastObstacleAlertDate) > 2.5 {
            self.obstacleMessage = nil
            statusMessage = "Room aligned to the live camera feed."
        }
    }

    private func renderRoom(at transform: simd_float4x4) {
        guard let arView else { return }

        if let existingAnchor = roomAnchor {
            arView.scene.anchors.remove(existingAnchor)
        }

        let anchor = AnchorEntity(world: SIMD3<Float>(0, 0, 0))
        anchor.transform = Transform(matrix: transform)

        for element in RoomModelVisualization.elements(for: envelope, includeFloor: false) {
            let mesh = MeshResource.generateBox(size: element.dimensions)
            let material = SimpleMaterial(color: visualizationColor(for: element.kind), isMetallic: false)
            let entity = ModelEntity(mesh: mesh, materials: [material])
            entity.name = element.label
            entity.transform = Transform(matrix: element.transform)
            anchor.addChild(entity)
        }

        let markerMesh = MeshResource.generateBox(size: 0.12)
        let markerMaterial = SimpleMaterial(
            color: UIColor(red: 1.0, green: 0.84, blue: 0.0, alpha: 0.9),
            isMetallic: false
        )
        let marker = ModelEntity(mesh: markerMesh, materials: [markerMaterial])
        marker.name = "Saved Room Origin"
        marker.position = [0, 0.06, 0]
        anchor.addChild(marker)

        arView.scene.anchors.append(anchor)
        roomAnchor = anchor

        // Imported USDZ meshes stay in the preview surface.
        // Live navigation uses the lighter semantic room shell to keep AR stable.
    }

    private func loadImportedVisualMesh(from url: URL, into anchor: AnchorEntity) {
        do {
            let visualMesh = try Entity.load(contentsOf: url)
            visualMesh.name = "ImportedVisualMesh"
            visualMesh.transform = .identity

            if roomAnchor === anchor {
                anchor.addChild(visualMesh)
                statusMessage = "Room aligned to the live camera feed. Visual mesh loaded."
            }
        } catch {
            if roomAnchor === anchor {
                statusMessage = "Room aligned, but the imported USDZ could not be loaded."
            }
        }
    }

    private func renderPath(clearOnly: Bool = false) {
        guard let arView else { return }

        if let guideAnchor {
            arView.scene.anchors.remove(guideAnchor)
            self.guideAnchor = nil
        }

        guard !clearOnly, isNavigationActive, !renderedPath.isEmpty else { return }

        let anchor = AnchorEntity(world: SIMD3<Float>(0, 0, 0))
        let arrowPoses = NavigationGuidanceMath.arrowPoses(for: renderedPath, spacing: 0.8)

        for pose in arrowPoses {
            let arrow = makeArrowEntity(at: pose)
            anchor.addChild(arrow)
        }

        arView.scene.anchors.append(anchor)
        guideAnchor = anchor
    }

    private func makeArrowEntity(at pose: NavigationArrowPose) -> Entity {
        let material = SimpleMaterial(
            color: UIColor(red: 1.0, green: 0.84, blue: 0.0, alpha: 0.85),
            isMetallic: false
        )

        let group = Entity()
        group.transform = Transform(
            scale: .one,
            rotation: simd_quatf(angle: pose.yawRadians, axis: SIMD3<Float>(0, 1, 0)),
            translation: pose.position + SIMD3<Float>(0, 0.02, 0)
        )

        let shaft = ModelEntity(
            mesh: MeshResource.generateBox(size: SIMD3<Float>(0.08, 0.01, 0.30)),
            materials: [material]
        )
        shaft.position = [0, 0, 0.02]

        let leftFin = ModelEntity(
            mesh: MeshResource.generateBox(size: SIMD3<Float>(0.05, 0.01, 0.18)),
            materials: [material]
        )
        leftFin.position = [-0.05, 0, 0.13]
        leftFin.orientation = simd_quatf(angle: -.pi / 4, axis: SIMD3<Float>(0, 1, 0))

        let rightFin = ModelEntity(
            mesh: MeshResource.generateBox(size: SIMD3<Float>(0.05, 0.01, 0.18)),
            materials: [material]
        )
        rightFin.position = [0.05, 0, 0.13]
        rightFin.orientation = simd_quatf(angle: .pi / 4, axis: SIMD3<Float>(0, 1, 0))

        group.addChild(shaft)
        group.addChild(leftFin)
        group.addChild(rightFin)
        return group
    }

    private func visualizationColor(for kind: RoomVisualizationKind) -> UIColor {
        switch kind {
        case .floor:
            return UIColor(white: 0.16, alpha: 0.18)
        case .wall:
            return UIColor(white: 0.82, alpha: 0.45)
        case .door(let isEntry):
            return isEntry
                ? UIColor(red: 1.0, green: 0.84, blue: 0.0, alpha: 0.85)
                : UIColor(red: 0.35, green: 0.72, blue: 0.98, alpha: 0.72)
        case .window:
            return UIColor(red: 0.47, green: 0.87, blue: 0.98, alpha: 0.32)
        case .object:
            return UIColor(red: 1.0, green: 0.58, blue: 0.21, alpha: 0.86)
        }
    }

    private func facingSurfaceName(
        for cameraTransform: simd_float4x4,
        roomWorldTransform: simd_float4x4
    ) -> String {
        let worldRoomTransform = roomWorldTransform.inverse
        let cameraWorldPosition = RoomGeometry.translation(of: cameraTransform)
        let roomPosition4 = worldRoomTransform * SIMD4<Float>(cameraWorldPosition.x, cameraWorldPosition.y, cameraWorldPosition.z, 1)
        let roomPosition = SIMD2<Float>(roomPosition4.x, roomPosition4.z)

        let forwardWorld = SIMD3<Float>(
            -cameraTransform.columns.2.x,
            0,
            -cameraTransform.columns.2.z
        )
        let forwardRoom4 = worldRoomTransform * SIMD4<Float>(forwardWorld.x, 0, forwardWorld.z, 0)
        let forwardRoom = SIMD2<Float>(forwardRoom4.x, forwardRoom4.z)
        guard simd_length_squared(forwardRoom) > 0.0001 else {
            return "--"
        }

        let rayDirection = simd_normalize(forwardRoom)
        var bestHit: (distance: Float, priority: Int, label: String)?

        for door in envelope.capturedRoomSnapshot.doors {
            let label = RoomLabeling.displayName(for: door)
            registerFacingHit(
                from: roomPosition,
                direction: rayDirection,
                surface: door,
                label: label,
                priority: 0,
                currentBest: &bestHit
            )
        }

        for wall in envelope.capturedRoomSnapshot.walls {
            let label = RoomLabeling.displayName(for: wall)
            registerFacingHit(
                from: roomPosition,
                direction: rayDirection,
                surface: wall,
                label: label,
                priority: 1,
                currentBest: &bestHit
            )
        }

        return bestHit?.label ?? CompassUtilities.directionString(for: trackerState.heading)
    }

    private func registerFacingHit(
        from origin: SIMD2<Float>,
        direction: SIMD2<Float>,
        surface: SurfaceSnapshot,
        label: String,
        priority: Int,
        currentBest: inout (distance: Float, priority: Int, label: String)?
    ) {
        let endpoints = RoomGeometry.surfaceEndpoints(
            transform: surface.transformMatrix.simd,
            width: surface.dimensionsMeters.x
        )
        let start = SIMD2<Float>(endpoints.0.x, endpoints.0.z)
        let end = SIMD2<Float>(endpoints.1.x, endpoints.1.z)

        guard let distance = rayIntersectionDistance(origin: origin, direction: direction, segmentStart: start, segmentEnd: end) else {
            return
        }

        if let best = currentBest {
            if distance < best.distance - 0.05 || (abs(distance - best.distance) <= 0.05 && priority < best.priority) {
                currentBest = (distance, priority, label)
            }
        } else {
            currentBest = (distance, priority, label)
        }
    }

    private func rayIntersectionDistance(
        origin: SIMD2<Float>,
        direction: SIMD2<Float>,
        segmentStart: SIMD2<Float>,
        segmentEnd: SIMD2<Float>
    ) -> Float? {
        let segmentVector = segmentEnd - segmentStart
        let denominator = cross2D(direction, segmentVector)
        guard abs(denominator) > 0.0001 else { return nil }

        let startDelta = segmentStart - origin
        let rayDistance = cross2D(startDelta, segmentVector) / denominator
        let segmentDistance = cross2D(startDelta, direction) / denominator

        guard rayDistance >= 0, segmentDistance >= 0, segmentDistance <= 1 else {
            return nil
        }

        return rayDistance
    }

    private func cross2D(_ lhs: SIMD2<Float>, _ rhs: SIMD2<Float>) -> Float {
        lhs.x * rhs.y - lhs.y * rhs.x
    }

    private func guidanceInstruction(
        turnAngle: Float,
        distanceToNext: Float,
        distanceRemaining: Float,
        destinationName: String
    ) -> String {
        if let turn = NavigationGuidanceMath.turnInstruction(for: turnAngle) {
            let direction = turn == .left ? "left" : "right"
            if distanceToNext <= 0.8 {
                return "Turn \(direction) now."
            }
            return String(format: "In %.1f meters, turn %@.", distanceToNext, direction)
        }

        if distanceRemaining <= 1.2 {
            return String(format: "%@ is %.1f meters ahead. Keep going forward.", destinationName, max(distanceRemaining, 0))
        }

        return String(format: "Walk forward %.1f meters toward %@.", distanceRemaining, destinationName)
    }

    private func guidanceAnnouncementKey(
        turnAngle: Float,
        distanceRemaining: Float,
        destinationName: String
    ) -> String {
        if let turn = NavigationGuidanceMath.turnInstruction(for: turnAngle) {
            let direction = turn == .left ? "left" : "right"
            return "turn-\(direction)-\(remainingWaypoints.count)"
        }

        if distanceRemaining <= 1.2 {
            return "approach-\(destinationName)"
        }

        let bucket = max(0, Int(floor(distanceRemaining / 1.5)))
        return "forward-\(destinationName)-\(bucket)"
    }

    private func pathWithDestination(_ path: [SIMD3<Float>], destination: SIMD3<Float>) -> [SIMD3<Float>] {
        guard let last = path.last else { return [destination] }
        if NavigationGuidanceMath.planarDistance(last, destination) <= 0.25 {
            return path
        }
        return path + [destination]
    }

    private func guidancePathForChecks(
        currentPosition: SIMD3<Float>,
        destination: SIMD3<Float>
    ) -> [SIMD3<Float>] {
        var points: [SIMD3<Float>] = []
        points.append(activePathOrigin ?? currentPosition)
        points.append(contentsOf: remainingWaypoints)
        if NavigationGuidanceMath.planarDistance(points.last ?? destination, destination) > 0.25 {
            points.append(destination)
        }
        return points
    }

    private func formattedDistance(_ distance: Float) -> String {
        String(format: "%.1f m", max(0, distance))
    }

    private func applyFrontObstacleWarning(_ warning: FrontObstacleWarning?) {
        guard isNavigationActive else {
            obstacleMessage = nil
            lastObstacleLabel = nil
            return
        }

        guard let warning else {
            if Date().timeIntervalSince(lastObstacleAlertDate) > 2.5 {
                obstacleMessage = nil
                lastObstacleLabel = nil
            }
            return
        }

        obstacleMessage = warning.message
        statusMessage = warning.message

        let shouldSpeak = lastObstacleLabel != warning.label || Date().timeIntervalSince(lastObstacleAlertDate) > 3.0
        if shouldSpeak {
            lastObstacleLabel = warning.label
            lastObstacleAlertDate = Date()
            speechEngine.speak(warning.message)
        }
    }
}

struct RoomAnchoringView: View {
    let envelope: RoomModelEnvelope
    let visualMeshURL: URL?
    let onRestart: () -> Void

    @StateObject private var controller: RoomAnchoringController

    init(envelope: RoomModelEnvelope, visualMeshURL: URL? = nil, onRestart: @escaping () -> Void) {
        self.envelope = envelope
        self.visualMeshURL = visualMeshURL
        self.onRestart = onRestart
        _controller = StateObject(wrappedValue: RoomAnchoringController(envelope: envelope, visualMeshURL: visualMeshURL))
    }

    var body: some View {
        ZStack {
            RoomAnchoringARView(controller: controller)
                .ignoresSafeArea()

            VStack(spacing: 0) {
                topStatusPanel
                    .padding(.top)
                    .padding(.horizontal)

                Spacer()
                
                if controller.isNavigationActive, let currentCam = controller.currentCameraTransform, let roomTransform = controller.roomWorldTransform {
                    HStack {
                        Spacer()
                        MiniMapView(
                            snapshot: envelope.capturedRoomSnapshot,
                            currentPosition: RoomGeometry.translation(of: currentCam),
                            currentCameraTransform: currentCam,
                            path: controller.renderedPath,
                            roomWorldTransform: roomTransform
                        )
                        .frame(width: 120, height: 120)
                        .padding(.trailing, 12)
                        .padding(.bottom, 8)
                    }
                    .transition(.move(edge: .trailing).combined(with: .opacity))
                }

                bottomControlPanel
                    .padding(.horizontal)
                    .padding(.bottom)
            }
        }
        .animation(.easeInOut(duration: 0.3), value: controller.isNavigationActive)
    }

    private var topStatusPanel: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(controller.isReady ? "Room Aligned" : "Aligning…")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.white)
                Spacer()
                trackingBadge
            }
            .accessibilityElement(children: .combine)

            if controller.isNavigationActive {
                NavigationStatsHUD(
                    distanceRemaining: controller.distanceRemainingText,
                    distanceWalked: controller.trackerState.distanceWalked,
                    heading: controller.trackerState.heading,
                    startingPoint: controller.activeStartName ?? "Current Location",
                    destinationName: controller.activeDestinationName,
                    instruction: controller.currentInstruction,
                    facingText: controller.facingSurfaceName
                )
            } else {
                Text(controller.currentInstruction)
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(.white)
                    .accessibilityLabel(controller.currentInstruction)
            }

            if let obstacleMessage = controller.obstacleMessage {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text(obstacleMessage)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                }
                .accessibilityLabel(obstacleMessage)
            }
        }
        .padding(10)
        .background(.black.opacity(0.78))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color.white.opacity(0.12), lineWidth: 1)
        )
    }

    private var bottomControlPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            if controller.isReady {
                NavigationAssistantView(
                    messages: $controller.conversationMessages,
                    destinationNames: controller.destinationNames,
                    roomContext: controller.roomContextSummary,
                    roomContextJSON: controller.roomContextJSON,
                    onNavigationRequested: { sourceName, destinationName in
                        controller.startNavigation(from: sourceName, to: destinationName)
                    },
                    onStopNavigation: {
                        controller.stopNavigation()
                    },
                    isNavigationActive: controller.isNavigationActive
                )
            } else {
                Button(action: controller.alignUsingCurrentCameraPose) {
                    Text("Align Here")
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(DotsPrimaryButtonStyle())
                .disabled(!matchesReadyState)
                .accessibilityLabel("Align room at current position")
                .accessibilityHint("Stand at the starting point and tap to align the saved room model.")
            }

            Button(action: onRestart) {
                Text("Switch Room")
                    .frame(maxWidth: .infinity, minHeight: 40)
            }
            .buttonStyle(DotsSecondaryButtonStyle())
            .accessibilityLabel("Switch to a different saved room model")
        }
        .padding(10)
        .background(.black.opacity(0.78))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color.white.opacity(0.12), lineWidth: 1)
        )
    }


    private var trackingBadge: some View {
        let label: String
        let color: Color

        switch trackingQualityText {
        case "Normal":
            label = trackingQualityText
            color = .green
        case "Limited":
            label = trackingQualityText
            color = .yellow
        default:
            label = trackingQualityText
            color = .orange
        }

        return Text(label)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(color.opacity(0.12))
            .clipShape(Capsule())
    }

    private var trackingQualityText: String {
        switch controller.trackingQuality {
        case .normal:
            return "Normal"
        case .limited:
            return "Limited"
        case .initializing:
            return "Initializing"
        }
    }

    private var matchesReadyState: Bool {
        if case .ready = TrackingGate.decision(for: controller.trackingQuality) {
            return true
        }
        return false
    }
}

struct RoomAnchoringARView: UIViewRepresentable {
    @ObservedObject var controller: RoomAnchoringController

    final class Coordinator {
        weak var controller: RoomAnchoringController?

        init(controller: RoomAnchoringController) {
            self.controller = controller
        }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(controller: controller)
    }

    func makeUIView(context: Context) -> ARView {
        let arView = ARView(frame: .zero)
        arView.automaticallyConfigureSession = false
        controller.attach(arView: arView)
        controller.startSession()
        return arView
    }

    func updateUIView(_ uiView: ARView, context: Context) {}

    static func dismantleUIView(_ uiView: ARView, coordinator: Coordinator) {
        coordinator.controller?.stopSession()
        uiView.session.pause()
    }
}
