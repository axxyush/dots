import Foundation

struct SavedRoomModelSummary: Identifiable, Equatable {
    let roomID: String
    let fileURL: URL
    let savedAt: Date
    let doorCount: Int
    let objectCount: Int
    let visualMeshURL: URL?
    let floorPlanImageURL: URL?

    var id: String { roomID }

    var title: String {
        "Room \(roomID.prefix(8))"
    }

    var hasVisualMesh: Bool {
        visualMeshURL != nil
    }

    var hasFloorPlan: Bool {
        floorPlanImageURL != nil
    }

    var visualMeshFilename: String? {
        visualMeshURL?.lastPathComponent
    }
}

enum LocalRoomModelStoreError: LocalizedError {
    case missingRoomID
    case missingStoredFile

    var errorDescription: String? {
        switch self {
        case .missingRoomID:
            return "The room model is missing its local identifier."
        case .missingStoredFile:
            return "The saved room model could not be found on this device."
        }
    }
}

final class LocalRoomModelStore {
    static let shared = LocalRoomModelStore()

    private let fileManager = FileManager.default
    private let visualMeshFilename = "visual_mesh.usdz"

    private init() {}

    func save(envelope: RoomModelEnvelope) throws -> SavedRoomModelSummary {
        let roomID = (envelope.roomID?.isEmpty == false ? envelope.roomID : nil) ?? UUID().uuidString.lowercased()
        let normalizedEnvelope = envelope.withRoomID(roomID)
        let request = RoomModelEnvelopeUploadRequest(roomModelEnvelope: normalizedEnvelope)

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.keyEncodingStrategy = .convertToSnakeCase

        let data = try encoder.encode(request)
        let fileURL = try storageDirectory().appendingPathComponent("\(roomID).json")
        try data.write(to: fileURL, options: .atomic)

        return summary(for: normalizedEnvelope, fileURL: fileURL)
    }

    func fetchSavedModels() throws -> [SavedRoomModelSummary] {
        let directory = try storageDirectory()
        let fileURLs = try fileManager.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: [.creationDateKey, .contentModificationDateKey],
            options: [.skipsHiddenFiles]
        )

        let summaries = try fileURLs
            .filter { $0.pathExtension.lowercased() == "json" }
            .map { url -> SavedRoomModelSummary in
                let envelope = try loadEnvelope(from: url)
                return summary(for: envelope, fileURL: url)
            }
            .sorted { $0.savedAt > $1.savedAt }

        return summaries
    }

    func loadEnvelope(roomID: String) throws -> RoomModelEnvelope {
        let fileURL = try storageDirectory().appendingPathComponent("\(roomID).json")
        guard fileManager.fileExists(atPath: fileURL.path) else {
            throw LocalRoomModelStoreError.missingStoredFile
        }
        return try loadEnvelope(from: fileURL)
    }

    func importVisualMesh(from sourceURL: URL, for roomID: String) throws -> URL {
        let directory = try visualMeshDirectory(for: roomID, createIfNeeded: true)
        let destinationURL = directory.appendingPathComponent(visualMeshFilename)

        let accessed = sourceURL.startAccessingSecurityScopedResource()
        defer {
            if accessed {
                sourceURL.stopAccessingSecurityScopedResource()
            }
        }

        if fileManager.fileExists(atPath: destinationURL.path) {
            try fileManager.removeItem(at: destinationURL)
        }

        try fileManager.copyItem(at: sourceURL, to: destinationURL)
        return destinationURL
    }

    /// Permanently deletes a saved room model and all associated assets (visual mesh, floor plan).
    func delete(roomID: String) throws {
        // Remove the JSON file
        let jsonURL = try storageDirectory().appendingPathComponent("\(roomID).json")
        if fileManager.fileExists(atPath: jsonURL.path) {
            try fileManager.removeItem(at: jsonURL)
        }

        // Remove the room's asset directory (visual mesh, floor plan, etc.)
        let assetDir = try storageDirectory().appendingPathComponent(roomID, isDirectory: true)
        if fileManager.fileExists(atPath: assetDir.path) {
            try fileManager.removeItem(at: assetDir)
        }
    }

    func visualMeshURL(roomID: String) -> URL? {
        guard let directory = try? visualMeshDirectory(for: roomID, createIfNeeded: false) else {
            return nil
        }

        let candidate = directory.appendingPathComponent(visualMeshFilename)
        return fileManager.fileExists(atPath: candidate.path) ? candidate : nil
    }

    private func loadEnvelope(from fileURL: URL) throws -> RoomModelEnvelope {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let data = try Data(contentsOf: fileURL)
        let request = try decoder.decode(RoomModelEnvelopeUploadRequest.self, from: data)
        return request.roomModelEnvelope
    }

    private func summary(for envelope: RoomModelEnvelope, fileURL: URL) -> SavedRoomModelSummary {
        let values = try? fileURL.resourceValues(forKeys: [.creationDateKey, .contentModificationDateKey])
        let savedAt = values?.creationDate ?? values?.contentModificationDate ?? Date()
        let resolvedRoomID = envelope.roomID ?? UUID().uuidString.lowercased()

        return SavedRoomModelSummary(
            roomID: resolvedRoomID,
            fileURL: fileURL,
            savedAt: savedAt,
            doorCount: envelope.capturedRoomSnapshot.doors.count,
            objectCount: envelope.capturedRoomSnapshot.objects.count,
            visualMeshURL: visualMeshURL(roomID: resolvedRoomID),
            floorPlanImageURL: FloorPlanExporter.floorPlanImageURL(roomID: resolvedRoomID)
        )
    }

    private func storageDirectory() throws -> URL {
        let baseDirectory = try fileManager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let roomModelsDirectory = baseDirectory.appendingPathComponent("RoomModels", isDirectory: true)
        if !fileManager.fileExists(atPath: roomModelsDirectory.path) {
            try fileManager.createDirectory(at: roomModelsDirectory, withIntermediateDirectories: true)
        }
        return roomModelsDirectory
    }

    private func visualMeshDirectory(for roomID: String, createIfNeeded: Bool) throws -> URL {
        let directory = try storageDirectory()
            .appendingPathComponent(roomID, isDirectory: true)
            .appendingPathComponent("VisualAssets", isDirectory: true)

        if createIfNeeded, !fileManager.fileExists(atPath: directory.path) {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        }

        return directory
    }
}
