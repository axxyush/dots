import XCTest
import simd
@testable import TestRoomPlan

final class RoomNavigationLogicTests: XCTestCase {
    func testDestinationResolverBuildsSemanticDestinationsAndChoosesNearestMatch() {
        let envelope = makeEnvelope()
        let resolver = RoomDestinationResolver(envelope: envelope, roomWorldTransform: matrix_identity_float4x4)

        XCTAssertEqual(resolver.destinationNames, ["Bathroom", "Seating Area", "Bed", "Exit"])

        resolver.updateUserWorldPosition(SIMD3<Float>(3.0, 0, 3.1))
        let bed = resolver.resolveDestination("bed")
        XCTAssertNotNil(bed)
        XCTAssertEqual(bed?.x ?? 0, 3.0, accuracy: 0.15)
        XCTAssertEqual(bed?.z ?? 0, 3.0, accuracy: 0.15)

        let bathroom = resolver.resolveDestination("restroom")
        XCTAssertNotNil(bathroom)
        XCTAssertEqual(bathroom?.x ?? 0, 3.15, accuracy: 0.25)
    }

    func testNavigatorFindsPathAroundInflatedObstacle() {
        let envelope = makePathEnvelope()
        let navigator = RoomNavigator(
            snapshot: envelope.capturedRoomSnapshot,
            roomWorldTransform: matrix_identity_float4x4
        )

        let start = SIMD3<Float>(0.4, 0, 0.4)
        let end = SIMD3<Float>(3.6, 0, 3.6)
        let path = navigator.findPath(from: start, to: end)

        XCTAssertFalse(path.isEmpty)
        XCTAssertEqual(path.first?.x ?? 0, 0.45, accuracy: 0.15)
        XCTAssertEqual(path.first?.z ?? 0, 0.45, accuracy: 0.15)
        XCTAssertEqual(path.last?.x ?? 0, 3.55, accuracy: 0.15)
        XCTAssertEqual(path.last?.z ?? 0, 3.55, accuracy: 0.15)

        let totalDistance = zip(path, path.dropFirst()).reduce(Float.zero) { partial, pair in
            partial + NavigationGuidanceMath.planarDistance(pair.0, pair.1)
        }

        XCTAssertGreaterThan(totalDistance, 4.6)
        XCTAssertTrue(path.allSatisfy { navigator.isWalkable(worldPoint: $0) })
    }

    func testGuidanceMathComputesTurnDistanceAndArrowPoses() {
        let camera = makeTransform(translation: SIMD3<Float>(0, 1.6, 0), yawDegrees: 0)
        let current = SIMD3<Float>(0, 0, 0)
        let target = SIMD3<Float>(-1, 0, 1)

        let angle = NavigationGuidanceMath.turnAngleDegrees(
            currentHeadingTransform: camera,
            targetWorldPoint: target,
            currentWorldPoint: current
        )

        XCTAssertGreaterThan(angle, 25)
        XCTAssertEqual(NavigationGuidanceMath.turnInstruction(for: angle), .left)

        let poses = NavigationGuidanceMath.arrowPoses(
            for: [SIMD3<Float>(0, 0, 0), SIMD3<Float>(0, 0, 2.4)],
            spacing: 0.8
        )
        XCTAssertEqual(poses.count, 3)
        XCTAssertEqual(poses.first?.position.z ?? 0, 0.4, accuracy: 0.15)
    }

    private func makeEnvelope() -> RoomModelEnvelope {
        let walls = [
            surface(index: 0, category: "wall", position: SIMD3<Float>(2.0, 1.5, 0.0), width: 4.0, height: 3.0, yawDegrees: 0),
            surface(index: 1, category: "wall", position: SIMD3<Float>(2.0, 1.5, 4.0), width: 4.0, height: 3.0, yawDegrees: 0),
            surface(index: 2, category: "wall", position: SIMD3<Float>(0.0, 1.5, 2.0), width: 4.0, height: 3.0, yawDegrees: 90),
            surface(index: 3, category: "wall", position: SIMD3<Float>(4.0, 1.5, 2.0), width: 4.0, height: 3.0, yawDegrees: 90)
        ]

        let door = surface(index: 0, category: "door", position: SIMD3<Float>(2.0, 1.05, 0.0), width: 0.9, height: 2.1, yawDegrees: 0)

        let objects = [
            object(index: 0, category: "table", position: SIMD3<Float>(2.0, 0.375, 2.0), dimensions: SIMD3<Float>(1.0, 0.75, 0.8), yawDegrees: 0),
            object(index: 1, category: "chair", position: SIMD3<Float>(2.6, 0.45, 2.0), dimensions: SIMD3<Float>(0.45, 0.9, 0.45), yawDegrees: 0),
            object(index: 2, category: "sink", position: SIMD3<Float>(3.2, 0.45, 1.2), dimensions: SIMD3<Float>(0.6, 0.9, 0.5), yawDegrees: 0),
            object(index: 3, category: "toilet", position: SIMD3<Float>(3.1, 0.45, 1.7), dimensions: SIMD3<Float>(0.6, 0.9, 0.7), yawDegrees: 0),
            object(index: 4, category: "bed", position: SIMD3<Float>(1.0, 0.3, 3.0), dimensions: SIMD3<Float>(1.8, 0.6, 2.0), yawDegrees: 0),
            object(index: 5, category: "bed", position: SIMD3<Float>(3.0, 0.3, 3.0), dimensions: SIMD3<Float>(1.8, 0.6, 2.0), yawDegrees: 0)
        ]

        let snapshot = CapturedRoomSnapshot(
            originTransform: TransformMatrixData(matrix_identity_float4x4),
            roomBounds: RoomSpatialBounds(minX: 0, maxX: 4, minY: 0, maxY: 3, minZ: 0, maxZ: 4),
            walls: walls,
            doors: [door],
            windows: [],
            objects: objects
        )

        return RoomModelEnvelope(
            schemaVersion: "1.0",
            roomID: "nav-room",
            marker: MarkerSnapshot(templateVersion: "local", physicalWidthMeters: 0.10, uuid: "nav-room"),
            entryAnchor: EntryAnchorSnapshot(
                doorIndex: 0,
                transformMatrix: door.transformMatrix,
                positionMeters: Vector3Data(x: 2.0, y: 1.05, z: 0.0)
            ),
            capturedRoomSnapshot: snapshot
        )
    }

    private func makePathEnvelope() -> RoomModelEnvelope {
        let walls = [
            surface(index: 0, category: "wall", position: SIMD3<Float>(2.0, 1.5, 0.0), width: 4.0, height: 3.0, yawDegrees: 0),
            surface(index: 1, category: "wall", position: SIMD3<Float>(2.0, 1.5, 4.0), width: 4.0, height: 3.0, yawDegrees: 0),
            surface(index: 2, category: "wall", position: SIMD3<Float>(0.0, 1.5, 2.0), width: 4.0, height: 3.0, yawDegrees: 90),
            surface(index: 3, category: "wall", position: SIMD3<Float>(4.0, 1.5, 2.0), width: 4.0, height: 3.0, yawDegrees: 90)
        ]

        let door = surface(index: 0, category: "door", position: SIMD3<Float>(2.0, 1.05, 0.0), width: 0.9, height: 2.1, yawDegrees: 0)
        let obstacle = object(
            index: 0,
            category: "table",
            position: SIMD3<Float>(2.0, 0.375, 2.0),
            dimensions: SIMD3<Float>(1.0, 0.75, 0.8),
            yawDegrees: 0
        )

        let snapshot = CapturedRoomSnapshot(
            originTransform: TransformMatrixData(matrix_identity_float4x4),
            roomBounds: RoomSpatialBounds(minX: 0, maxX: 4, minY: 0, maxY: 3, minZ: 0, maxZ: 4),
            walls: walls,
            doors: [door],
            windows: [],
            objects: [obstacle]
        )

        return RoomModelEnvelope(
            schemaVersion: "1.0",
            roomID: "path-room",
            marker: MarkerSnapshot(templateVersion: "local", physicalWidthMeters: 0.10, uuid: "path-room"),
            entryAnchor: EntryAnchorSnapshot(
                doorIndex: 0,
                transformMatrix: door.transformMatrix,
                positionMeters: Vector3Data(x: 2.0, y: 1.05, z: 0.0)
            ),
            capturedRoomSnapshot: snapshot
        )
    }

    private func surface(
        index: Int,
        category: String,
        position: SIMD3<Float>,
        width: Float,
        height: Float,
        yawDegrees: Float
    ) -> SurfaceSnapshot {
        SurfaceSnapshot(
            index: index,
            category: category,
            dimensionsMeters: Vector3Data(x: width, y: height, z: 0),
            transformMatrix: TransformMatrixData(makeTransform(translation: position, yawDegrees: yawDegrees))
        )
    }

    private func object(
        index: Int,
        category: String,
        position: SIMD3<Float>,
        dimensions: SIMD3<Float>,
        yawDegrees: Float
    ) -> ObjectSnapshot {
        ObjectSnapshot(
            index: index,
            category: category,
            dimensionsMeters: Vector3Data(dimensions),
            transformMatrix: TransformMatrixData(makeTransform(translation: position, yawDegrees: yawDegrees)),
            confidence: "high"
        )
    }

    private func makeTransform(translation: SIMD3<Float>, yawDegrees: Float) -> simd_float4x4 {
        let radians = yawDegrees * (.pi / 180)
        return simd_float4x4(
            SIMD4<Float>(cos(radians), 0, -sin(radians), 0),
            SIMD4<Float>(0, 1, 0, 0),
            SIMD4<Float>(sin(radians), 0, cos(radians), 0),
            SIMD4<Float>(translation.x, translation.y, translation.z, 1)
        )
    }
}
