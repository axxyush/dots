import Foundation

struct ScanExportData: Codable {
    let metadata: ScanMetadata
    let walls: [WallData]
    let doors: [DoorWindowData]
    let windows: [DoorWindowData]
    let objects: [ObjectData]
    let distanceMatrix: [DistancePair]
    let accuracyReport: AccuracyReport
}

struct ScanMetadata: Codable {
    let timestamp: String
    let scanDurationSeconds: Double
    let totalWalls: Int
    let totalDoors: Int
    let totalWindows: Int
    let totalObjects: Int
    let roomWidthMeters: Double
    let roomDepthMeters: Double
    let boundingBox: RoomBoundingBox
}

struct RoomBoundingBox: Codable {
    let minX: Double
    let maxX: Double
    let minY: Double
    let maxY: Double
    let minZ: Double
    let maxZ: Double
}

struct WallData: Codable {
    let index: Int
    let positionX: Double
    let positionY: Double
    let positionZ: Double
    let widthMeters: Double
    let heightMeters: Double
    let rotationQuaternion: QuaternionData
}

struct DoorWindowData: Codable {
    let index: Int
    let category: String
    let positionX: Double
    let positionY: Double
    let positionZ: Double
    let widthMeters: Double
    let heightMeters: Double
    let rotationQuaternion: QuaternionData
    let parentWallIndex: Int?
}

struct ObjectData: Codable {
    let index: Int
    let category: String
    let positionX: Double
    let positionY: Double
    let positionZ: Double
    let widthMeters: Double
    let heightMeters: Double
    let depthMeters: Double
    let confidence: String
}

struct QuaternionData: Codable {
    let x: Double
    let y: Double
    let z: Double
    let w: Double
}

struct DistancePair: Codable {
    let objectA: String
    let objectAIndex: Int
    let objectB: String
    let objectBIndex: Int
    let floorDistanceMeters: Double
}

struct AccuracyReport: Codable {
    let detectedRoomSize: String
    let wallCount: Int
    let doorCount: Int
    let windowCount: Int
    let objectCount: Int
    let scanDurationSeconds: Double
    let objectInventory: [String]
    let distanceReport: [String]
}
