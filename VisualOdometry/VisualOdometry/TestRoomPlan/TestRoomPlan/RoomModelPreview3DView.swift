import SwiftUI
import SceneKit
import UIKit

// MARK: - Camera Mode

enum RoomPreviewCameraMode: Equatable {
    case orbit     // Default: orbit around the room (bird's eye)
    case firstPerson  // Google Street View-style: look around from a point inside the room
}

enum RoomPreviewCameraPreset {
    case overview
    case entryDoor
}

// MARK: - Tappable Element

struct TappableRoomElement: Identifiable, Equatable {
    let id: String
    let label: String
    let position: SIMD3<Float>
    let kind: RoomVisualizationKind

    static func == (lhs: TappableRoomElement, rhs: TappableRoomElement) -> Bool {
        lhs.id == rhs.id
    }
}

// MARK: - Preview View

struct RoomModelPreview3DView: View {
    let envelope: RoomModelEnvelope
    let cameraPreset: RoomPreviewCameraPreset
    let visualMeshURL: URL?

    @State private var cameraMode: RoomPreviewCameraMode = .orbit
    @State private var tappedElement: TappableRoomElement?
    @State private var showModeToggle = true

    init(
        envelope: RoomModelEnvelope,
        cameraPreset: RoomPreviewCameraPreset = .overview,
        visualMeshURL: URL? = nil
    ) {
        self.envelope = envelope
        self.cameraPreset = cameraPreset
        self.visualMeshURL = visualMeshURL
    }

    var body: some View {
        ZStack {
            InteractiveRoomSceneView(
                envelope: envelope,
                cameraPreset: cameraPreset,
                visualMeshURL: visualMeshURL,
                cameraMode: $cameraMode,
                tappedElement: $tappedElement
            )
            .ignoresSafeArea()

            VStack {
                // Top controls
                HStack {
                    Spacer()

                    // Camera mode toggle
                    if showModeToggle {
                        Button {
                            withAnimation(.easeInOut(duration: 0.25)) {
                                cameraMode = (cameraMode == .orbit) ? .firstPerson : .orbit
                            }
                        } label: {
                            HStack(spacing: 6) {
                                Image(systemName: cameraMode == .orbit ? "eye" : "cube.transparent")
                                    .font(.system(size: 14, weight: .semibold))
                                Text(cameraMode == .orbit ? "First Person" : "Orbit View")
                                    .font(.caption.weight(.semibold))
                            }
                            .foregroundStyle(.white)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 8)
                            .background(.black.opacity(0.7))
                            .clipShape(Capsule())
                            .overlay(Capsule().stroke(Color.white.opacity(0.2), lineWidth: 1))
                        }
                        .padding(.trailing, 16)
                        .padding(.top, 8)
                    }
                }

                Spacer()

                // Bottom info
                if let element = tappedElement {
                    HStack(spacing: 8) {
                        Image(systemName: iconForKind(element.kind))
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(colorForKind(element.kind))
                        Text(element.label)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.white)

                        Spacer()

                        if cameraMode == .orbit {
                            Text("Tap to enter")
                                .font(.caption)
                                .foregroundStyle(.white.opacity(0.6))
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(.black.opacity(0.78))
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                    .padding(.horizontal, 16)
                    .padding(.bottom, 12)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                }

                if cameraMode == .firstPerson {
                    Text("Drag to look around · Pinch to zoom · Tap objects to fly to them")
                        .font(.caption2)
                        .foregroundStyle(.white.opacity(0.5))
                        .padding(.bottom, 8)
                }
            }
        }
        .background(Color.black)
        .accessibilityLabel("Interactive 3D room preview")
        .accessibilityHint("Tap objects to fly into them. Switch between orbit and first-person views.")
    }

    private func iconForKind(_ kind: RoomVisualizationKind) -> String {
        switch kind {
        case .door: return "door.left.hand.open"
        case .window: return "window.vertical.open"
        case .wall: return "square.fill"
        case .floor: return "square.grid.3x3.fill"
        case .object(let cat):
            switch cat.lowercased() {
            case "chair": return "chair.fill"
            case "table": return "table.furniture.fill"
            case "bed": return "bed.double.fill"
            case "sofa", "couch": return "sofa.fill"
            default: return "cube.fill"
            }
        }
    }

    private func colorForKind(_ kind: RoomVisualizationKind) -> Color {
        switch kind {
        case .door(let isEntry): return isEntry ? .yellow : .blue
        case .window: return .cyan
        case .wall: return .gray
        case .floor: return .gray
        case .object: return .orange
        }
    }
}

// MARK: - Interactive SCNView (UIViewRepresentable)

struct InteractiveRoomSceneView: UIViewRepresentable {
    let envelope: RoomModelEnvelope
    let cameraPreset: RoomPreviewCameraPreset
    let visualMeshURL: URL?
    @Binding var cameraMode: RoomPreviewCameraMode
    @Binding var tappedElement: TappableRoomElement?

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> SCNView {
        let scnView = SCNView()
        scnView.backgroundColor = .black
        scnView.antialiasingMode = .multisampling4X
        scnView.autoenablesDefaultLighting = true

        // Build scene
        let result = InteractiveSceneBuilder.makeScene(
            for: envelope,
            cameraPreset: cameraPreset,
            visualMeshURL: visualMeshURL
        )
        scnView.scene = result.scene
        scnView.pointOfView = result.cameraNode

        context.coordinator.scnView = scnView
        context.coordinator.orbitCameraNode = result.cameraNode
        context.coordinator.fpCameraNode = result.fpCameraNode
        context.coordinator.tappableElements = result.tappableElements
        context.coordinator.roomBounds = envelope.capturedRoomSnapshot.roomBounds

        // Orbit mode by default
        scnView.allowsCameraControl = true
        scnView.defaultCameraController.interactionMode = .orbitTurntable
        scnView.defaultCameraController.minimumVerticalAngle = -80
        scnView.defaultCameraController.maximumVerticalAngle = 80

        // Tap gesture
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.handleTap(_:)))
        scnView.addGestureRecognizer(tap)

        // Pan gesture for first-person look
        let pan = UIPanGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.handlePan(_:)))
        pan.minimumNumberOfTouches = 1
        pan.maximumNumberOfTouches = 1
        context.coordinator.fpPanGesture = pan
        // Don't add yet — only in first-person mode

        return scnView
    }

    func updateUIView(_ uiView: SCNView, context: Context) {
        let coordinator = context.coordinator

        if cameraMode != coordinator.currentMode {
            coordinator.currentMode = cameraMode
            coordinator.switchCameraMode(to: cameraMode)
        }
    }

    // MARK: - Coordinator

    class Coordinator: NSObject {
        let parent: InteractiveRoomSceneView
        var scnView: SCNView?
        var orbitCameraNode: SCNNode?
        var fpCameraNode: SCNNode?
        var fpPanGesture: UIPanGestureRecognizer?
        var tappableElements: [TappableRoomElement] = []
        var roomBounds: RoomSpatialBounds?
        var currentMode: RoomPreviewCameraMode = .orbit

        // First-person look state
        private var fpYaw: Float = 0
        private var fpPitch: Float = 0

        init(parent: InteractiveRoomSceneView) {
            self.parent = parent
        }

        @objc func handleTap(_ gesture: UITapGestureRecognizer) {
            guard let scnView else { return }
            let location = gesture.location(in: scnView)

            let hitResults = scnView.hitTest(location, options: [
                .searchMode: SCNHitTestSearchMode.all.rawValue,
                .ignoreHiddenNodes: false
            ])

            for hit in hitResults {
                let nodeName = hit.node.name ?? hit.node.parent?.name ?? ""
                if let element = tappableElements.first(where: { $0.id == nodeName || $0.label == nodeName }) {
                    Task { @MainActor in
                        self.parent.tappedElement = element
                    }
                    flyToElement(element)
                    return
                }
            }

            // Tapped empty space — clear selection
            Task { @MainActor in
                self.parent.tappedElement = nil
            }
        }

        @objc func handlePan(_ gesture: UIPanGestureRecognizer) {
            guard let scnView, let fpCamera = fpCameraNode, currentMode == .firstPerson else { return }

            let translation = gesture.translation(in: scnView)
            let sensitivity: Float = 0.003

            fpYaw -= Float(translation.x) * sensitivity
            fpPitch -= Float(translation.y) * sensitivity
            fpPitch = max(-.pi / 2.5, min(.pi / 2.5, fpPitch))

            fpCamera.eulerAngles = SCNVector3(fpPitch, fpYaw, 0)
            gesture.setTranslation(.zero, in: scnView)
        }

        func flyToElement(_ element: TappableRoomElement) {
            guard scnView != nil else { return }

            let targetPosition = element.position
            let eyeHeight: Float = 1.55

            // Calculate a position slightly offset from the element, looking at it
            let roomCenter = roomCenterPoint()
            let dirToCenter = simd_normalize(roomCenter - targetPosition)

            // Position the camera slightly in front of the element, facing into the room
            let cameraPosition: SIMD3<Float>
            let lookTarget: SIMD3<Float>

            switch element.kind {
            case .door:
                // Stand at the door, looking into the room
                cameraPosition = SIMD3<Float>(
                    targetPosition.x + dirToCenter.x * 0.3,
                    eyeHeight,
                    targetPosition.z + dirToCenter.z * 0.3
                )
                lookTarget = SIMD3<Float>(roomCenter.x, eyeHeight - 0.1, roomCenter.z)
            case .object:
                // Stand 1.2m away from the object, looking at it
                let standoff: Float = 1.2
                cameraPosition = SIMD3<Float>(
                    targetPosition.x - dirToCenter.x * standoff,
                    eyeHeight,
                    targetPosition.z - dirToCenter.z * standoff
                )
                lookTarget = SIMD3<Float>(targetPosition.x, targetPosition.y, targetPosition.z)
            default:
                // Walls/windows — stand 1m away
                cameraPosition = SIMD3<Float>(
                    targetPosition.x + dirToCenter.x * 1.0,
                    eyeHeight,
                    targetPosition.z + dirToCenter.z * 1.0
                )
                lookTarget = SIMD3<Float>(targetPosition.x, targetPosition.y, targetPosition.z)
            }

            // Switch to first-person mode and animate
            Task { @MainActor in
                self.parent.cameraMode = .firstPerson
            }

            // Use FP camera for the fly-in
            guard let fpCamera = fpCameraNode else { return }

            // Calculate yaw/pitch to look at target
            let dir = lookTarget - cameraPosition
            let yaw = atan2(-dir.x, -dir.z)
            let horizontalDist = sqrt(dir.x * dir.x + dir.z * dir.z)
            let pitch = atan2(dir.y, horizontalDist)

            SCNTransaction.begin()
            SCNTransaction.animationDuration = 0.9
            SCNTransaction.animationTimingFunction = CAMediaTimingFunction(name: .easeInEaseOut)

            fpCamera.position = SCNVector3(cameraPosition.x, cameraPosition.y, cameraPosition.z)
            fpCamera.eulerAngles = SCNVector3(pitch, yaw, 0)

            SCNTransaction.completionBlock = {
                self.fpYaw = yaw
                self.fpPitch = pitch
            }

            SCNTransaction.commit()

            // Switch the point of view
            switchCameraMode(to: .firstPerson)
        }

        func switchCameraMode(to mode: RoomPreviewCameraMode) {
            guard let scnView else { return }

            switch mode {
            case .orbit:
                scnView.allowsCameraControl = true
                scnView.defaultCameraController.interactionMode = .orbitTurntable

                // Remove FP pan gesture
                if let pan = fpPanGesture {
                    scnView.removeGestureRecognizer(pan)
                }

                // Animate back to orbit camera
                if let orbitCam = orbitCameraNode {
                    SCNTransaction.begin()
                    SCNTransaction.animationDuration = 0.6
                    scnView.pointOfView = orbitCam
                    SCNTransaction.commit()
                }

            case .firstPerson:
                scnView.allowsCameraControl = false

                // Add FP pan gesture for look-around
                if let pan = fpPanGesture, !scnView.gestureRecognizers!.contains(pan) {
                    scnView.addGestureRecognizer(pan)
                }

                // Switch to FP camera
                if let fpCam = fpCameraNode {
                    // If we haven't positioned it yet, put it at the entry door
                    if fpCam.position.x == 0, fpCam.position.y == 0, fpCam.position.z == 0 {
                        let entryPos = parent.envelope.entryAnchor.positionMeters.simd
                        let entryTransform = parent.envelope.entryAnchor.transformMatrix.simd
                        let forward = RoomGeometry.axis(of: entryTransform, column: 2)
                        let eyeHeight: Float = 1.55

                        fpCam.position = SCNVector3(entryPos.x, eyeHeight, entryPos.z)
                        let lookDir = SIMD3<Float>(forward.x, 0, forward.z)
                        let yaw = atan2(-lookDir.x, -lookDir.z)
                        fpCam.eulerAngles = SCNVector3(0, yaw, 0)
                        fpYaw = yaw
                        fpPitch = 0
                    }

                    SCNTransaction.begin()
                    SCNTransaction.animationDuration = 0.6
                    scnView.pointOfView = fpCam
                    SCNTransaction.commit()
                }
            }
        }

        private func roomCenterPoint() -> SIMD3<Float> {
            guard let bounds = roomBounds else { return .zero }
            return SIMD3<Float>(
                (bounds.minX + bounds.maxX) / 2,
                (bounds.minY + bounds.maxY) / 2,
                (bounds.minZ + bounds.maxZ) / 2
            )
        }
    }
}

// MARK: - Scene Builder

private enum InteractiveSceneBuilder {
    struct SceneResult {
        let scene: SCNScene
        let cameraNode: SCNNode
        let fpCameraNode: SCNNode
        let tappableElements: [TappableRoomElement]
    }

    static func makeScene(
        for envelope: RoomModelEnvelope,
        cameraPreset: RoomPreviewCameraPreset,
        visualMeshURL: URL?
    ) -> SceneResult {
        let scene = SCNScene()
        scene.background.contents = UIColor.black

        let semanticRootNode = SCNNode()
        semanticRootNode.name = "SavedRoomRoot"

        var tappableElements: [TappableRoomElement] = []

        for element in RoomModelVisualization.elements(for: envelope, includeFloor: true) {
            let node = makeNode(for: element)
            semanticRootNode.addChildNode(node)

            let position = RoomGeometry.translation(of: element.transform)
            tappableElements.append(TappableRoomElement(
                id: element.id,
                label: element.label,
                position: position,
                kind: element.kind
            ))
        }

        if let importedMeshNode = importedMeshNode(from: visualMeshURL) {
            importedMeshNode.name = "ImportedVisualMesh"
            scene.rootNode.addChildNode(importedMeshNode)
            semanticRootNode.opacity = 0.38
        }

        scene.rootNode.addChildNode(semanticRootNode)
        scene.rootNode.addChildNode(ambientLightNode())
        scene.rootNode.addChildNode(fillLightNode())

        // Orbit camera
        let targetNode = SCNNode()
        targetNode.name = "CameraTarget"
        let pose = cameraPose(for: envelope, preset: cameraPreset)
        targetNode.simdPosition = pose.target
        scene.rootNode.addChildNode(targetNode)

        let orbitCamera = makeOrbitCameraNode(pose: pose, targetNode: targetNode)
        scene.rootNode.addChildNode(orbitCamera)

        // First-person camera (positioned at entry door initially)
        let fpCamera = makeFirstPersonCameraNode()
        fpCamera.name = "FPCamera"
        scene.rootNode.addChildNode(fpCamera)

        return SceneResult(
            scene: scene,
            cameraNode: orbitCamera,
            fpCameraNode: fpCamera,
            tappableElements: tappableElements
        )
    }

    private static func importedMeshNode(from url: URL?) -> SCNNode? {
        guard let url else { return nil }
        guard let meshScene = try? SCNScene(url: url, options: nil) else { return nil }
        let container = SCNNode()
        for child in meshScene.rootNode.childNodes {
            container.addChildNode(child.clone())
        }
        return container
    }

    private static func makeNode(for element: RoomVisualizationElement) -> SCNNode {
        let box = SCNBox(
            width: CGFloat(element.dimensions.x),
            height: CGFloat(element.dimensions.y),
            length: CGFloat(element.dimensions.z),
            chamferRadius: CGFloat(min(element.dimensions.x, element.dimensions.z) * 0.04)
        )
        box.firstMaterial = material(for: element.kind)

        let node = SCNNode(geometry: box)
        node.name = element.id
        node.simdTransform = element.transform
        return node
    }

    private static func material(for kind: RoomVisualizationKind) -> SCNMaterial {
        let mat = SCNMaterial()
        mat.diffuse.contents = color(for: kind)
        mat.metalness.contents = 0.08
        mat.roughness.contents = 0.55
        mat.lightingModel = .physicallyBased
        mat.transparency = transparency(for: kind)
        mat.isDoubleSided = true
        return mat
    }

    private static func color(for kind: RoomVisualizationKind) -> UIColor {
        switch kind {
        case .floor:
            return UIColor(white: 0.18, alpha: 1.0)
        case .wall:
            return UIColor(white: 0.78, alpha: 1.0)
        case .door(let isEntry):
            return isEntry
                ? UIColor(red: 1.0, green: 0.84, blue: 0.0, alpha: 1.0)
                : UIColor(red: 0.38, green: 0.71, blue: 0.95, alpha: 1.0)
        case .window:
            return UIColor(red: 0.47, green: 0.87, blue: 0.98, alpha: 1.0)
        case .object(let category):
            switch category.lowercased() {
            case "chair":
                return UIColor(red: 0.99, green: 0.62, blue: 0.18, alpha: 1.0)
            case "table":
                return UIColor(red: 0.85, green: 0.43, blue: 0.19, alpha: 1.0)
            case "bed":
                return UIColor(red: 0.66, green: 0.52, blue: 0.95, alpha: 1.0)
            case "sofa", "couch":
                return UIColor(red: 0.49, green: 0.74, blue: 0.44, alpha: 1.0)
            default:
                return UIColor(red: 1.0, green: 0.58, blue: 0.21, alpha: 1.0)
            }
        }
    }

    private static func transparency(for kind: RoomVisualizationKind) -> CGFloat {
        switch kind {
        case .floor: return 0.55
        case .wall: return 0.72
        case .door: return 0.88
        case .window: return 0.38
        case .object: return 0.9
        }
    }

    private static func ambientLightNode() -> SCNNode {
        let node = SCNNode()
        let light = SCNLight()
        light.type = .ambient
        light.intensity = 700
        light.color = UIColor(white: 0.86, alpha: 1.0)
        node.light = light
        return node
    }

    private static func fillLightNode() -> SCNNode {
        let node = SCNNode()
        let light = SCNLight()
        light.type = .omni
        light.intensity = 1100
        light.color = UIColor(white: 0.98, alpha: 1.0)
        node.light = light
        node.position = SCNVector3(1.5, 3.8, 2.6)
        return node
    }

    private static func makeOrbitCameraNode(pose: RoomPreviewCameraPose, targetNode: SCNNode) -> SCNNode {
        let cameraNode = SCNNode()
        let camera = SCNCamera()
        camera.zFar = 100
        camera.wantsHDR = true
        cameraNode.camera = camera
        cameraNode.simdPosition = pose.position
        let constraint = SCNLookAtConstraint(target: targetNode)
        constraint.isGimbalLockEnabled = true
        cameraNode.constraints = [constraint]
        return cameraNode
    }

    private static func makeFirstPersonCameraNode() -> SCNNode {
        let node = SCNNode()
        let camera = SCNCamera()
        camera.zFar = 100
        camera.zNear = 0.05
        camera.fieldOfView = 75
        camera.wantsHDR = true
        node.camera = camera
        // Position will be set when first-person mode is activated
        return node
    }

    private static func cameraPose(
        for envelope: RoomModelEnvelope,
        preset: RoomPreviewCameraPreset
    ) -> RoomPreviewCameraPose {
        switch preset {
        case .overview:
            return RoomModelVisualization.previewCameraPose(for: envelope.capturedRoomSnapshot.roomBounds)
        case .entryDoor:
            let entryTransform = envelope.entryAnchor.transformMatrix.simd
            let entryPosition = RoomGeometry.translation(of: entryTransform)
            let forward = RoomGeometry.axis(of: entryTransform, column: 2)
            let eyeHeight: Float = 1.55
            let position = SIMD3<Float>(
                entryPosition.x,
                max(entryPosition.y, eyeHeight),
                entryPosition.z
            )
            let target = position + forward * 1.6 + SIMD3<Float>(0, 0.05, 0)
            return RoomPreviewCameraPose(position: position, target: target)
        }
    }
}
