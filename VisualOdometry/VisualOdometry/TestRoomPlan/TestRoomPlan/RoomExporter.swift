import Foundation
import RoomPlan
import simd

enum RoomExporter {

    // MARK: - Main Export

    static func export(room: CapturedRoom, scanDuration: TimeInterval) -> ScanExportData {
        let walls = room.walls.enumerated().map { i, s in wallData(index: i, surface: s) }
        let doors = room.doors.enumerated().map { i, s in doorWindowData(index: i, surface: s, label: "Door", walls: room.walls) }
        let windows = room.windows.enumerated().map { i, s in doorWindowData(index: i, surface: s, label: "Window", walls: room.walls) }
        let objects = room.objects.enumerated().map { i, o in objectData(index: i, object: o) }

        let (bbox, roomWidth, roomDepth) = computeRoomDimensions(walls: room.walls)
        let distanceMatrix = computeDistanceMatrix(objects: objects)
        let report = buildReport(
            walls: walls, doors: doors, windows: windows,
            objects: objects, distanceMatrix: distanceMatrix,
            roomWidth: roomWidth, roomDepth: roomDepth,
            scanDuration: scanDuration
        )

        let formatter = ISO8601DateFormatter()
        let metadata = ScanMetadata(
            timestamp: formatter.string(from: Date()),
            scanDurationSeconds: scanDuration,
            totalWalls: walls.count,
            totalDoors: doors.count,
            totalWindows: windows.count,
            totalObjects: objects.count,
            roomWidthMeters: roomWidth,
            roomDepthMeters: roomDepth,
            boundingBox: bbox
        )

        return ScanExportData(
            metadata: metadata,
            walls: walls,
            doors: doors,
            windows: windows,
            objects: objects,
            distanceMatrix: distanceMatrix,
            accuracyReport: report
        )
    }

    // MARK: - JSON File

    static func saveJSON(_ data: ScanExportData) -> URL? {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let jsonData = try? encoder.encode(data) else { return nil }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let filename = "roomplan_scan_\(formatter.string(from: Date())).json"

        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let url = dir.appendingPathComponent(filename)
        do {
            try jsonData.write(to: url)
            return url
        } catch {
            return nil
        }
    }

    // MARK: - Surface / Object Conversion

    private static func wallData(index: Int, surface: CapturedRoom.Surface) -> WallData {
        let pos = RoomGeometry.translation(of: surface.transform)
        let quat = quaternion(from: surface.transform)
        return WallData(
            index: index,
            positionX: Double(pos.x),
            positionY: Double(pos.y),
            positionZ: Double(pos.z),
            widthMeters: Double(surface.dimensions.x),
            heightMeters: Double(surface.dimensions.y),
            rotationQuaternion: quat
        )
    }

    private static func doorWindowData(index: Int, surface: CapturedRoom.Surface, label: String, walls: [CapturedRoom.Surface]) -> DoorWindowData {
        let pos = RoomGeometry.translation(of: surface.transform)
        let quat = quaternion(from: surface.transform)
        return DoorWindowData(
            index: index,
            category: label,
            positionX: Double(pos.x),
            positionY: Double(pos.y),
            positionZ: Double(pos.z),
            widthMeters: Double(surface.dimensions.x),
            heightMeters: Double(surface.dimensions.y),
            rotationQuaternion: quat,
            parentWallIndex: nearestWallIndex(for: surface, in: walls)
        )
    }

    private static func objectData(index: Int, object: CapturedRoom.Object) -> ObjectData {
        let pos = RoomGeometry.translation(of: object.transform)
        return ObjectData(
            index: index,
            category: objectCategoryName(object.category),
            positionX: Double(pos.x),
            positionY: Double(pos.y),
            positionZ: Double(pos.z),
            widthMeters: Double(object.dimensions.x),
            heightMeters: Double(object.dimensions.y),
            depthMeters: Double(object.dimensions.z),
            confidence: confidenceName(object.confidence)
        )
    }

    // MARK: - Geometry Helpers

    private static func quaternion(from transform: simd_float4x4) -> QuaternionData {
        let rot = simd_float3x3(
            simd_float3(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
            simd_float3(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
            simd_float3(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
        )
        let q = simd_quatf(rot)
        return QuaternionData(x: Double(q.imag.x), y: Double(q.imag.y), z: Double(q.imag.z), w: Double(q.real))
    }

    private static func nearestWallIndex(for surface: CapturedRoom.Surface, in walls: [CapturedRoom.Surface]) -> Int? {
        guard !walls.isEmpty else { return nil }
        let pos = RoomGeometry.translation(of: surface.transform)
        var bestIndex = 0
        var bestDist: Float = .infinity
        for (i, wall) in walls.enumerated() {
            let wp = RoomGeometry.translation(of: wall.transform)
            let d = simd_distance(pos, wp)
            if d < bestDist { bestDist = d; bestIndex = i }
        }
        return bestIndex
    }

    static func computeRoomDimensions(walls: [CapturedRoom.Surface]) -> (RoomBoundingBox, Double, Double) {
        guard !walls.isEmpty else {
            return (RoomBoundingBox(minX: 0, maxX: 0, minY: 0, maxY: 0, minZ: 0, maxZ: 0), 0, 0)
        }
        let wallSegments = walls.map { RoomGeometry.surfaceEndpoints(transform: $0.transform, width: $0.dimensions.x) }
        let bounds = RoomGeometry.bounds(
            surfaces: wallSegments,
            objectFootprints: [],
            includeOrigin: false,
            padding: 0
        )
        let bbox = RoomBoundingBox(
            minX: Double(bounds.minX), maxX: Double(bounds.maxX),
            minY: Double(bounds.minY), maxY: Double(bounds.maxY),
            minZ: Double(bounds.minZ), maxZ: Double(bounds.maxZ)
        )
        return (bbox, Double(bounds.widthMeters), Double(bounds.depthMeters))
    }

    // MARK: - Distance Matrix

    static func computeDistanceMatrix(objects: [ObjectData]) -> [DistancePair] {
        var pairs: [DistancePair] = []
        for i in 0..<objects.count {
            for j in (i + 1)..<objects.count {
                let dx = objects[i].positionX - objects[j].positionX
                let dz = objects[i].positionZ - objects[j].positionZ
                pairs.append(DistancePair(
                    objectA: "\(objects[i].category) #\(i)",
                    objectAIndex: i,
                    objectB: "\(objects[j].category) #\(j)",
                    objectBIndex: j,
                    floorDistanceMeters: sqrt(dx * dx + dz * dz)
                ))
            }
        }
        return pairs
    }

    // MARK: - Report

    private static func buildReport(
        walls: [WallData], doors: [DoorWindowData], windows: [DoorWindowData],
        objects: [ObjectData], distanceMatrix: [DistancePair],
        roomWidth: Double, roomDepth: Double, scanDuration: TimeInterval
    ) -> AccuracyReport {
        let sizeStr = String(format: "Detected room size: %.2f m x %.2f m", roomWidth, roomDepth)

        let inventory = objects.map { o in
            String(format: "Object: %@ at position (%.2f, %.2f, %.2f), size: %.2f x %.2f x %.2f m",
                   o.category, o.positionX, o.positionY, o.positionZ,
                   o.widthMeters, o.depthMeters, o.heightMeters)
        }

        let distReport = distanceMatrix.map { p in
            String(format: "Distance from %@ to %@: %.2f m", p.objectA, p.objectB, p.floorDistanceMeters)
        }

        return AccuracyReport(
            detectedRoomSize: sizeStr,
            wallCount: walls.count,
            doorCount: doors.count,
            windowCount: windows.count,
            objectCount: objects.count,
            scanDurationSeconds: scanDuration,
            objectInventory: inventory,
            distanceReport: distReport
        )
    }

    // MARK: - Category Names

    static func objectCategoryName(_ category: CapturedRoom.Object.Category) -> String {
        switch category {
        case .storage:      return "Storage"
        case .refrigerator: return "Refrigerator"
        case .stove:        return "Stove"
        case .bed:          return "Bed"
        case .sink:         return "Sink"
        case .washerDryer:  return "Washer/Dryer"
        case .toilet:       return "Toilet"
        case .bathtub:      return "Bathtub"
        case .oven:         return "Oven"
        case .dishwasher:   return "Dishwasher"
        case .table:        return "Table"
        case .sofa:         return "Sofa"
        case .chair:        return "Chair"
        case .fireplace:    return "Fireplace"
        case .television:   return "Television"
        case .stairs:       return "Stairs"
        @unknown default:   return "Unknown"
        }
    }
    
    static func confidenceName(_ confidence: CapturedRoom.Confidence) -> String {
        switch confidence {
        case .low:
            return "Low"
        case .medium:
            return "Medium"
        case .high:
            return "High"
        @unknown default:
            return "Unknown"
        }
    }

}
