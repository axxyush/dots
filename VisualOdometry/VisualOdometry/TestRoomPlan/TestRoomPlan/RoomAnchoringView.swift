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
    @Published private(set) var activeDestinationName: String?
    @Published var statusMessage = "Stand at the saved entry door and face into the room."
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
    private var obstacleDetector: DynamicObstacleDetector?
    private let speechEngine = NavigationSpeechEngine()
    private let hapticsEngine = NavigationHapticsEngine()
    private var currentCameraTransform: simd_float4x4?
    private var destinationWorldPoint: SIMD3<Float>?
    private var remainingWaypoints: [SIMD3<Float>] = []
    private var renderedPath: [SIMD3<Float>] = []
    private var lastTurnAnnouncementKey: String?
    private var lastObstacleAlertDate = Date.distantPast
    private var lastRecalculationDate = Date.distantPast

    init(envelope: RoomModelEnvelope, visualMeshURL: URL? = nil) {
        self.envelope = envelope
        self.visualMeshURL = visualMeshURL
        super.init()
    }

    func attach(arView: ARView) {
        self.arView = arView
        arView.session.delegate = self
    }

    func startSession() {
        guard let arView else { return }

        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravityAndHeading // Enables compass heading
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification) {
            configuration.sceneReconstruction = .meshWithClassification
        }
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            configuration.frameSemantics.insert(.sceneDepth)
        }
        arView.session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
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

        destinationResolver = RoomDestinationResolver(envelope: envelope, roomWorldTransform: roomTransform)
        navigator = RoomNavigator(snapshot: envelope.capturedRoomSnapshot, roomWorldTransform: roomTransform)
        obstacleDetector = DynamicObstacleDetector(envelope: envelope, roomWorldTransform: roomTransform)
        destinationNames = destinationResolver?.destinationNames ?? []

        renderRoom(at: roomTransform)
        stopNavigation(resetInstruction: false)
    }

    func startNavigation(to destinationName: String) {
        guard
            isReady,
            let frame = arView?.session.currentFrame,
            let resolver = destinationResolver,
            let navigator
        else {
            return
        }

        let userPosition = RoomGeometry.translation(of: frame.camera.transform)
        resolver.updateUserWorldPosition(userPosition)

        guard let candidate = resolver.resolveCandidate(destinationName) else {
            currentInstruction = "Could not find that destination in the saved room model."
            return
        }

        let path = navigator.findPath(from: userPosition, to: candidate.worldPosition)
        guard !path.isEmpty else {
            currentInstruction = "Could not map a walkable route to \(candidate.name)."
            return
        }

        activeDestinationName = candidate.name
        destinationWorldPoint = candidate.worldPosition
        remainingWaypoints = path
        renderedPath = pathWithDestination(path, destination: candidate.worldPosition)
        isNavigationActive = true
        lastTurnAnnouncementKey = nil
        obstacleMessage = nil

        currentInstruction = "Starting navigation to \(candidate.name). Walk forward."
        distanceRemainingText = formattedDistance(
            NavigationGuidanceMath.distanceRemaining(
                currentWorldPoint: userPosition,
                remainingWaypoints: renderedPath
            )
        )

        renderPath()
        speechEngine.speak("Starting navigation to \(candidate.name). Walk forward.")
    }

    func stopNavigation(resetInstruction: Bool = true) {
        isNavigationActive = false
        activeDestinationName = nil
        destinationWorldPoint = nil
        remainingWaypoints = []
        renderedPath = []
        distanceRemainingText = "--"
        lastTurnAnnouncementKey = nil
        obstacleMessage = nil

        if let guideAnchor, let arView {
            arView.scene.anchors.remove(guideAnchor)
            self.guideAnchor = nil
        }

        if resetInstruction {
            currentInstruction = isReady ? "Choose a destination to start navigation." : "Align the saved plan to the live camera feed."
        }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        Task { @MainActor in
            self.handleFrameUpdate(frame)
            // Update odometry tracker on every frame
            self.trackerProcessor.processFrame(frame)
        }
    }

    @MainActor
    private func handleFrameUpdate(_ frame: ARFrame) {
        trackingQuality = TrackingQuality(trackingState: frame.camera.trackingState)
        currentCameraTransform = frame.camera.transform

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

        let currentPosition = RoomGeometry.translation(of: frame.camera.transform)
        destinationResolver?.updateUserWorldPosition(currentPosition)
        statusMessage = "Room aligned to the live camera feed."

        if isNavigationActive {
            advanceNavigation(frame: frame, currentPosition: currentPosition)
        }
    }

    @MainActor
    private func advanceNavigation(frame: ARFrame, currentPosition: SIMD3<Float>) {
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
            remainingWaypoints.removeFirst()
        }

        if NavigationGuidanceMath.hasArrived(currentWorldPoint: currentPosition, destinationWorldPoint: destinationWorldPoint) {
            currentInstruction = "You have arrived at \(activeDestinationName)."
            distanceRemainingText = "0.0 m"
            renderPath(clearOnly: true)
            isNavigationActive = false
            speechEngine.speak("You have arrived at \(activeDestinationName).")
            hapticsEngine.playArrival()
            return
        }

        let pathForChecks = pathWithDestination(remainingWaypoints, destination: destinationWorldPoint)
        if NavigationGuidanceMath.isOffPath(currentWorldPoint: currentPosition, waypoints: pathForChecks) {
            if Date().timeIntervalSince(lastRecalculationDate) > 1.0 {
                lastRecalculationDate = Date()
                speechEngine.speak("Recalculating.")
                currentInstruction = "Recalculating."
                let updatedPath = navigator.findPath(from: currentPosition, to: destinationWorldPoint)
                remainingWaypoints = updatedPath
            }
        }

        renderedPath = pathWithDestination(remainingWaypoints, destination: destinationWorldPoint)
        renderPath()

        let nextWaypoint = remainingWaypoints.first ?? destinationWorldPoint
        let distanceToNext = NavigationGuidanceMath.planarDistance(currentPosition, nextWaypoint)
        let turnAngle = NavigationGuidanceMath.turnAngleDegrees(
            currentHeadingTransform: frame.camera.transform,
            targetWorldPoint: nextWaypoint,
            currentWorldPoint: currentPosition
        )

        if let turn = NavigationGuidanceMath.turnInstruction(for: turnAngle) {
            let direction = turn == .left ? "left" : "right"
            currentInstruction = String(format: "In %.1f meters, turn %@.", distanceToNext, direction)
            let roundedDistance = Int(distanceToNext * 10)
            let announcementKey = "\(direction)-\(roundedDistance)-\(remainingWaypoints.count)"
            if lastTurnAnnouncementKey != announcementKey {
                speechEngine.speak(currentInstruction)
                if turn == .left {
                    hapticsEngine.playLeftTurn()
                } else {
                    hapticsEngine.playRightTurn()
                }
                lastTurnAnnouncementKey = announcementKey
            }
        } else {
            currentInstruction = "Walk forward."
        }

        distanceRemainingText = formattedDistance(
            NavigationGuidanceMath.distanceRemaining(
                currentWorldPoint: currentPosition,
                remainingWaypoints: renderedPath
            )
        )

        if let obstacleDetector,
           let obstacleHit = obstacleDetector.detectObstacle(frame: frame),
           Date().timeIntervalSince(lastObstacleAlertDate) > 2.0 {
            lastObstacleAlertDate = Date()
            obstacleMessage = String(format: "Obstacle ahead (%.1f m).", obstacleHit.forwardDistance)
            speechEngine.speak("Obstacle ahead.")
            hapticsEngine.playObstacleAlert()
        } else if let obstacleMessage, Date().timeIntervalSince(lastObstacleAlertDate) > 2.5 {
            self.obstacleMessage = nil
            if obstacleMessage.contains("Obstacle ahead") {
                statusMessage = "Room aligned to the live camera feed."
            }
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

        if let visualMeshURL {
            loadImportedVisualMesh(from: visualMeshURL, into: anchor)
        }
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

    private func pathWithDestination(_ path: [SIMD3<Float>], destination: SIMD3<Float>) -> [SIMD3<Float>] {
        guard let last = path.last else { return [destination] }
        if NavigationGuidanceMath.planarDistance(last, destination) <= 0.25 {
            return path
        }
        return path + [destination]
    }

    private func formattedDistance(_ distance: Float) -> String {
        String(format: "%.1f m", max(0, distance))
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

                bottomControlPanel
                    .padding(.horizontal)
                    .padding(.bottom)
            }
        }
    }

    private var topStatusPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(controller.isReady ? "Aligned Room" : "Aligning Room")
                    .font(.headline)
                    .foregroundStyle(.white)
                Spacer()

                NavigationCompassView(heading: controller.trackerState.heading)

                trackingBadge
            }

            Text(controller.statusMessage)
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.9))

            if controller.isNavigationActive {
                // Google-Maps-like stats HUD during active navigation
                NavigationStatsHUD(
                    distanceRemaining: controller.distanceRemainingText,
                    distanceWalked: controller.trackerState.distanceWalked,
                    stepsTaken: controller.trackerState.stepsTaken,
                    heading: controller.trackerState.heading,
                    startingPoint: "Entry Door",
                    destinationName: controller.activeDestinationName,
                    instruction: controller.currentInstruction
                )
            } else {
                Text(controller.currentInstruction)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.white)

                if controller.isReady {
                    // Show compact odometry when aligned but not navigating
                    HStack(spacing: 16) {
                        statusChip(title: "Walked", value: String(format: "%.1f m", controller.trackerState.distanceWalked))
                        statusChip(title: "Steps", value: "\(controller.trackerState.stepsTaken)")
                        statusChip(title: "Heading", value: "\(Int(controller.trackerState.heading))° \(CompassUtilities.directionString(for: controller.trackerState.heading))")
                    }
                }
            }

            if let obstacleMessage = controller.obstacleMessage {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text(obstacleMessage)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.orange)
                }
            }

            Text("Gold marks the saved entry door. Yellow arrows show the planned path on the floor.")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.7))
        }
        .padding(16)
        .background(.black.opacity(0.84))
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(Color.white.opacity(0.14), lineWidth: 1)
        )
    }

    private var bottomControlPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let roomID = envelope.roomID {
                Text("Room ID: \(roomID)")
                    .font(.caption.monospaced())
                    .foregroundStyle(.white.opacity(0.68))
                    .textSelection(.enabled)
            }

            if visualMeshURL != nil {
                Text("Imported visual mesh attached")
                    .font(.caption)
                    .foregroundStyle(.green.opacity(0.92))
            }

            if controller.isReady {
                // Conversational assistant replaces destination buttons
                NavigationAssistantView(
                    messages: $controller.conversationMessages,
                    destinationNames: controller.destinationNames,
                    onDestinationChosen: { name in
                        controller.startNavigation(to: name)
                    },
                    onStopNavigation: {
                        controller.stopNavigation()
                    },
                    isNavigationActive: controller.isNavigationActive
                )
            } else {
                Button(action: controller.alignUsingCurrentCameraPose) {
                    Text("Align Here at Entry")
                        .frame(maxWidth: .infinity, minHeight: 60)
                }
                .buttonStyle(DotsPrimaryButtonStyle())
                .disabled(!matchesReadyState)
            }

            Button(action: onRestart) {
                Text("Choose Another Saved Model")
                    .frame(maxWidth: .infinity, minHeight: 60)
            }
            .buttonStyle(DotsSecondaryButtonStyle())
        }
        .padding(16)
        .background(.black.opacity(0.84))
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(Color.white.opacity(0.14), lineWidth: 1)
        )
    }

    private func statusChip(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.white.opacity(0.55))
            Text(value)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.white)
                .lineLimit(2)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color.white.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 12))
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

    func makeUIView(context: Context) -> ARView {
        let arView = ARView(frame: .zero)
        controller.attach(arView: arView)
        controller.startSession()
        return arView
    }

    func updateUIView(_ uiView: ARView, context: Context) {}

    static func dismantleUIView(_ uiView: ARView, coordinator: ()) {
        uiView.session.pause()
    }
}
