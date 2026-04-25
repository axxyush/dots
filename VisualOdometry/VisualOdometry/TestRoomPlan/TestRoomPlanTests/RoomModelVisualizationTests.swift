import XCTest
import simd
@testable import TestRoomPlan

final class RoomModelVisualizationTests: XCTestCase {
    func testBoundsFromBoxCornersPreserveVerticalExtent() {
        let wallTransform = makeTransform(translation: SIMD3<Float>(0, 1.4, 0), yawDegrees: 0)
        let objectTransform = makeTransform(translation: SIMD3<Float>(1.1, 0.45, -0.6), yawDegrees: 30)

        let wallCorners = RoomGeometry.orientedBoxCorners(
            transform: wallTransform,
            dimensions: SIMD3<Float>(4.0, 2.8, 0.0)
        )
        let objectCorners = RoomGeometry.orientedBoxCorners(
            transform: objectTransform,
            dimensions: SIMD3<Float>(0.8, 0.9, 0.6)
        )

        let bounds = RoomGeometry.bounds(
            boxCorners: [wallCorners, objectCorners],
            includeOrigin: false,
            padding: 0
        )

        XCTAssertEqual(bounds.minY, 0.0, accuracy: 0.0001)
        XCTAssertEqual(bounds.maxY, 2.8, accuracy: 0.0001)
        XCTAssertGreaterThan(bounds.maxX, 1.3)
        XCTAssertLessThan(bounds.minZ, -1.0)
    }

    func testVisualizationElementsIncludeFloorAndEntryDoor() {
        let envelope = makeEnvelope()
        let elements = RoomModelVisualization.elements(for: envelope, includeFloor: true)

        XCTAssertEqual(elements.count, 5)
        XCTAssertEqual(elements.filter { if case .floor = $0.kind { return true } else { return false } }.count, 1)
        XCTAssertEqual(elements.filter { if case .wall = $0.kind { return true } else { return false } }.count, 1)

        let entryDoors = elements.filter {
            if case .door(let isEntry) = $0.kind {
                return isEntry
            }
            return false
        }
        XCTAssertEqual(entryDoors.count, 1)
        XCTAssertEqual(entryDoors.first?.label, "Entry Door")
    }

    private func makeEnvelope() -> RoomModelEnvelope {
        let wall = SurfaceSnapshot(
            index: 0,
            category: "wall",
            dimensionsMeters: Vector3Data(x: 4.0, y: 2.8, z: 0),
            transformMatrix: TransformMatrixData(
                makeTransform(translation: SIMD3<Float>(0, 1.4, -2.0), yawDegrees: 0)
            )
        )
        let door = SurfaceSnapshot(
            index: 0,
            category: "door",
            dimensionsMeters: Vector3Data(x: 0.9, y: 2.1, z: 0),
            transformMatrix: TransformMatrixData(
                makeTransform(translation: SIMD3<Float>(-1.2, 1.05, 1.8), yawDegrees: 0)
            )
        )
        let window = SurfaceSnapshot(
            index: 0,
            category: "window",
            dimensionsMeters: Vector3Data(x: 1.1, y: 1.0, z: 0),
            transformMatrix: TransformMatrixData(
                makeTransform(translation: SIMD3<Float>(1.2, 1.5, -1.9), yawDegrees: 0)
            )
        )
        let object = ObjectSnapshot(
            index: 0,
            category: "table",
            dimensionsMeters: Vector3Data(x: 1.2, y: 0.75, z: 0.7),
            transformMatrix: TransformMatrixData(
                makeTransform(translation: SIMD3<Float>(0.4, 0.375, 0.2), yawDegrees: 18)
            ),
            confidence: "high"
        )

        let snapshot = CapturedRoomSnapshot(
            originTransform: TransformMatrixData(matrix_identity_float4x4),
            roomBounds: RoomSpatialBounds(
                minX: -2.2,
                maxX: 2.0,
                minY: 0.0,
                maxY: 2.8,
                minZ: -2.2,
                maxZ: 2.1
            ),
            walls: [wall],
            doors: [door],
            windows: [window],
            objects: [object]
        )

        return RoomModelEnvelope(
            schemaVersion: "1.0",
            roomID: "preview-room",
            marker: MarkerSnapshot(
                templateVersion: "local",
                physicalWidthMeters: 0.10,
                uuid: "preview-room"
            ),
            entryAnchor: EntryAnchorSnapshot(
                doorIndex: 0,
                transformMatrix: door.transformMatrix,
                positionMeters: Vector3Data(x: -1.2, y: 1.05, z: 1.8)
            ),
            capturedRoomSnapshot: snapshot
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
