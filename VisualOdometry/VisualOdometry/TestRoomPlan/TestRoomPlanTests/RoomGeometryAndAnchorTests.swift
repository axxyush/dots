import XCTest
import simd
@testable import TestRoomPlan

final class RoomGeometryAndAnchorTests: XCTestCase {
    func testTransformMatrixRoundTripPreservesTranslationAndYaw() {
        let transform = makeTransform(translation: SIMD3<Float>(1.25, 0.5, -2.75), yawDegrees: 37)
        let data = TransformMatrixData(transform)
        let decoded = data.simd

        XCTAssertEqual(decoded.columns.3.x, transform.columns.3.x, accuracy: 0.0001)
        XCTAssertEqual(decoded.columns.3.y, transform.columns.3.y, accuracy: 0.0001)
        XCTAssertEqual(decoded.columns.3.z, transform.columns.3.z, accuracy: 0.0001)
        XCTAssertEqual(yaw(of: decoded), yaw(of: transform), accuracy: 0.0001)
    }

    func testRoomWorldTransformRepositionsEntryAnchorToDetectedPlacard() {
        let entryAnchor = makeTransform(translation: SIMD3<Float>(0.8, 0, 0.25), yawDegrees: 15)
        let qrAnchorWorld = makeTransform(translation: SIMD3<Float>(3.4, 0, -1.2), yawDegrees: 72)

        let roomWorld = RoomAnchorMath.roomWorldTransform(
            qrAnchorWorld: qrAnchorWorld,
            entryAnchorRoom: entryAnchor
        )

        let resolvedEntryPose = roomWorld * entryAnchor
        XCTAssertMatrixEqual(resolvedEntryPose, qrAnchorWorld, accuracy: 0.0001)
    }

    func testOrientedFootprintUsesTransformBasis() {
        let transform = makeTransform(translation: SIMD3<Float>(2, 0, 3), yawDegrees: 90)
        let footprint = RoomGeometry.orientedFootprint(
            transform: transform,
            dimensions: SIMD3<Float>(2, 1, 1)
        )

        let xs = footprint.map(\.x).sorted()
        let zs = footprint.map(\.y).sorted()

        XCTAssertEqual(xs.first ?? 0, 1.5, accuracy: 0.0001)
        XCTAssertEqual(xs.last ?? 0, 2.5, accuracy: 0.0001)
        XCTAssertEqual(zs.first ?? 0, 2.0, accuracy: 0.0001)
        XCTAssertEqual(zs.last ?? 0, 4.0, accuracy: 0.0001)
    }

    func testTrackingGateRequiresNormalTracking() {
        XCTAssertEqual(TrackingGate.decision(for: .normal), .ready)
        XCTAssertEqual(
            TrackingGate.decision(for: .limited(reason: "Too much motion.")),
            .holdStill(message: "Hold still until tracking improves.")
        )
        XCTAssertEqual(
            TrackingGate.decision(for: .initializing),
            .holdStill(message: "Move slowly while ARKit establishes tracking.")
        )
    }

    private func makeTransform(translation: SIMD3<Float>, yawDegrees: Float) -> simd_float4x4 {
        let radians = yawDegrees * (.pi / 180)
        let rotation = simd_float4x4(
            SIMD4<Float>(cos(radians), 0, -sin(radians), 0),
            SIMD4<Float>(0, 1, 0, 0),
            SIMD4<Float>(sin(radians), 0, cos(radians), 0),
            SIMD4<Float>(translation.x, translation.y, translation.z, 1)
        )
        return rotation
    }

    private func yaw(of matrix: simd_float4x4) -> Float {
        atan2(matrix.columns.2.x, matrix.columns.2.z)
    }

    private func XCTAssertMatrixEqual(
        _ lhs: simd_float4x4,
        _ rhs: simd_float4x4,
        accuracy: Float,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let left = RoomGeometry.columnMajorArray(from: lhs)
        let right = RoomGeometry.columnMajorArray(from: rhs)
        for (l, r) in zip(left, right) {
            XCTAssertEqual(l, r, accuracy: accuracy, file: file, line: line)
        }
    }
}
