import SwiftUI
import RoomPlan
import simd
import CoreLocation

// MARK: - Floor Plan Metadata

struct FloorPlanMetadata: Codable {
    let roomID: String
    let compassHeadingAtScan: Double?
    let roomWidthMeters: Float
    let roomDepthMeters: Float
    let entryDoorIndex: Int
    let wallBearings: [WallBearing]
    let objectPositions: [ObjectPosition]
    let savedAt: String

    struct WallBearing: Codable {
        let index: Int
        let startX: Float
        let startZ: Float
        let endX: Float
        let endZ: Float
        let bearingDegrees: Double
    }

    struct ObjectPosition: Codable {
        let index: Int
        let category: String
        let roomX: Float
        let roomZ: Float
        let widthMeters: Float
        let depthMeters: Float
    }
}

// MARK: - Floor Plan Exporter

enum FloorPlanExporter {
    private static let floorPlanImageFilename = "floor_plan.png"
    private static let floorPlanMetadataFilename = "floor_plan_metadata.json"

    /// Renders the existing FloorPlanView into a UIImage at 3× scale.
    @MainActor
    static func renderFloorPlanImage(
        capturedRoom: CapturedRoom,
        selectedDoorIndex: Int?
    ) -> UIImage? {
        let planView = FloorPlanView(
            capturedRoom: capturedRoom,
            selectedDoorIndex: selectedDoorIndex
        )
        .frame(width: 800, height: 800)
        .background(Color.white)

        let renderer = ImageRenderer(content: planView)
        renderer.scale = 3
        return renderer.uiImage
    }

    /// Builds metadata containing compass-relative wall orientations and object positions.
    static func buildMetadata(
        capturedRoom: CapturedRoom,
        roomID: String,
        entryDoorIndex: Int,
        compassHeading: Double? = nil
    ) -> FloorPlanMetadata {
        let wallBearings: [FloorPlanMetadata.WallBearing] = capturedRoom.walls.enumerated().map { index, wall in
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: wall.transform, width: wall.dimensions.x)
            let dx = Double(b.x - a.x)
            let dz = Double(b.z - a.z)
            let bearing = atan2(dx, -dz) * 180.0 / .pi
            let normalizedBearing = bearing < 0 ? bearing + 360 : bearing

            return FloorPlanMetadata.WallBearing(
                index: index,
                startX: a.x,
                startZ: a.z,
                endX: b.x,
                endZ: b.z,
                bearingDegrees: normalizedBearing
            )
        }

        let objectPositions: [FloorPlanMetadata.ObjectPosition] = capturedRoom.objects.enumerated().map { index, obj in
            let pos = RoomGeometry.translation(of: obj.transform)
            return FloorPlanMetadata.ObjectPosition(
                index: index,
                category: RoomExporter.objectCategoryName(obj.category),
                roomX: pos.x,
                roomZ: pos.z,
                widthMeters: obj.dimensions.x,
                depthMeters: obj.dimensions.z
            )
        }

        let (_, roomWidth, roomDepth) = RoomExporter.computeRoomDimensions(walls: capturedRoom.walls)

        let formatter = ISO8601DateFormatter()

        return FloorPlanMetadata(
            roomID: roomID,
            compassHeadingAtScan: compassHeading,
            roomWidthMeters: Float(roomWidth),
            roomDepthMeters: Float(roomDepth),
            entryDoorIndex: entryDoorIndex,
            wallBearings: wallBearings,
            objectPositions: objectPositions,
            savedAt: formatter.string(from: Date())
        )
    }

    /// Saves the floor plan image (PNG) and metadata (JSON) to the room model directory.
    static func saveFloorPlan(
        image: UIImage,
        metadata: FloorPlanMetadata,
        roomID: String
    ) throws {
        let directory = try floorPlanDirectory(for: roomID, createIfNeeded: true)

        // Save PNG
        let imageURL = directory.appendingPathComponent(floorPlanImageFilename)
        guard let pngData = image.pngData() else {
            throw FloorPlanExportError.imageConversionFailed
        }
        try pngData.write(to: imageURL, options: .atomic)

        // Save metadata
        let metadataURL = directory.appendingPathComponent(floorPlanMetadataFilename)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let jsonData = try encoder.encode(metadata)
        try jsonData.write(to: metadataURL, options: .atomic)
    }

    /// Loads the saved floor plan image for a room.
    static func loadFloorPlanImage(roomID: String) -> UIImage? {
        guard let directory = try? floorPlanDirectory(for: roomID, createIfNeeded: false) else {
            return nil
        }
        let url = directory.appendingPathComponent(floorPlanImageFilename)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        guard let data = try? Data(contentsOf: url) else { return nil }
        return UIImage(data: data)
    }

    /// Loads the saved floor plan metadata for a room.
    static func loadFloorPlanMetadata(roomID: String) -> FloorPlanMetadata? {
        guard let directory = try? floorPlanDirectory(for: roomID, createIfNeeded: false) else {
            return nil
        }
        let url = directory.appendingPathComponent(floorPlanMetadataFilename)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(FloorPlanMetadata.self, from: data)
    }

    /// Returns the URL of the saved floor plan image, or nil if not saved.
    static func floorPlanImageURL(roomID: String) -> URL? {
        guard let directory = try? floorPlanDirectory(for: roomID, createIfNeeded: false) else {
            return nil
        }
        let url = directory.appendingPathComponent(floorPlanImageFilename)
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    // MARK: - Private

    private static func floorPlanDirectory(for roomID: String, createIfNeeded: Bool) throws -> URL {
        let base = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = base
            .appendingPathComponent("RoomModels", isDirectory: true)
            .appendingPathComponent(roomID, isDirectory: true)
            .appendingPathComponent("FloorPlan", isDirectory: true)

        if createIfNeeded, !FileManager.default.fileExists(atPath: directory.path) {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        }

        return directory
    }
}

// MARK: - Error

enum FloorPlanExportError: LocalizedError {
    case imageConversionFailed

    var errorDescription: String? {
        switch self {
        case .imageConversionFailed:
            return "Could not convert the floor plan to PNG data."
        }
    }
}
