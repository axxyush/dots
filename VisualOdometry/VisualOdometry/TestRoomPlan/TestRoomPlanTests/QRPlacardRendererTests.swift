import XCTest
@testable import TestRoomPlan

final class QRPlacardRendererTests: XCTestCase {
    func testPlacardRenderingIsDeterministic() {
        let uuid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

        let first = QRPlacardRenderer.renderPlacard(uuid: uuid)
        let second = QRPlacardRenderer.renderPlacard(uuid: uuid)

        XCTAssertNotNil(first)
        XCTAssertNotNil(second)
        XCTAssertEqual(first?.pngData, second?.pngData)
    }

    func testReferenceImageUsesStandardPhysicalWidth() {
        let uuid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        let referenceImage = QRPlacardRenderer.referenceImage(for: uuid)

        XCTAssertNotNil(referenceImage)
        XCTAssertEqual(referenceImage!.physicalSize.width, CGFloat(RoomModelExporter.markerPhysicalWidthMeters), accuracy: 0.0001)
    }
}
