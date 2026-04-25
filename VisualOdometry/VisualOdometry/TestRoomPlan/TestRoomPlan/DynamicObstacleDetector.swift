import ARKit
import Foundation
import simd

struct DynamicObstacleHit: Equatable {
    let worldPosition: SIMD3<Float>
    let forwardDistance: Float
}

final class DynamicObstacleDetector {
    private struct StaticVolume {
        let transform: simd_float4x4
        let dimensions: SIMD3<Float>

        func contains(_ point: SIMD3<Float>, margin: Float) -> Bool {
            let inverse = transform.inverse
            let local = inverse * SIMD4<Float>(point.x, point.y, point.z, 1)
            return abs(local.x) <= (dimensions.x / 2 + margin)
                && abs(local.y) <= (dimensions.y / 2 + margin)
                && abs(local.z) <= (dimensions.z / 2 + margin)
        }
    }

    private let floorWorldY: Float
    private let staticVolumes: [StaticVolume]

    init(envelope: RoomModelEnvelope, roomWorldTransform: simd_float4x4) {
        let snapshot = envelope.capturedRoomSnapshot
        let elements = RoomModelVisualization.elements(for: envelope, includeFloor: false)
        self.staticVolumes = elements.map { element in
            StaticVolume(
                transform: roomWorldTransform * element.transform,
                dimensions: element.dimensions
            )
        }

        let floorPoint = roomWorldTransform * SIMD4<Float>(0, snapshot.roomBounds.minY, 0, 1)
        self.floorWorldY = floorPoint.y
    }

    func detectObstacle(frame: ARFrame) -> DynamicObstacleHit? {
        let cameraTransform = frame.camera.transform
        let cameraInverse = cameraTransform.inverse
        let meshAnchors = frame.anchors.compactMap { $0 as? ARMeshAnchor }

        var closest: DynamicObstacleHit?

        for anchor in meshAnchors {
            let vertexCount = anchor.geometry.vertices.count
            guard vertexCount > 0 else { continue }

            let sampleStride = max(1, vertexCount / 80)
            var index = 0
            while index < vertexCount {
                let localVertex = anchor.geometry.vertex(at: UInt32(index))
                let worldVertex4 = anchor.transform * SIMD4<Float>(localVertex.x, localVertex.y, localVertex.z, 1)
                let worldPoint = SIMD3<Float>(worldVertex4.x, worldVertex4.y, worldVertex4.z)

                guard worldPoint.y - floorWorldY <= 2.0, worldPoint.y - floorWorldY >= 0.02 else {
                    index += sampleStride
                    continue
                }

                let cameraLocal = cameraInverse * SIMD4<Float>(worldPoint.x, worldPoint.y, worldPoint.z, 1)
                let forwardDistance = -cameraLocal.z
                let lateralDistance = abs(cameraLocal.x)

                guard forwardDistance > 0.15, forwardDistance <= 1.2, lateralDistance <= 0.6 else {
                    index += sampleStride
                    continue
                }

                let overlapsStaticModel = staticVolumes.contains { $0.contains(worldPoint, margin: 0.18) }
                if !overlapsStaticModel {
                    let hit = DynamicObstacleHit(worldPosition: worldPoint, forwardDistance: forwardDistance)
                    if closest == nil || hit.forwardDistance < closest!.forwardDistance {
                        closest = hit
                    }
                }

                index += sampleStride
            }
        }

        return closest
    }
}

private extension ARMeshGeometry {
    func vertex(at index: UInt32) -> SIMD3<Float> {
        let vertexPointer = vertices.buffer.contents().advanced(by: vertices.offset + vertices.stride * Int(index))
        let floatBuffer = vertexPointer.assumingMemoryBound(to: Float.self)
        return SIMD3<Float>(floatBuffer[0], floatBuffer[1], floatBuffer[2])
    }
}
