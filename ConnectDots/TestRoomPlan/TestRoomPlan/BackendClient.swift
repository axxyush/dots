import Foundation
import UIKit

// TODO: Update kBaseURL as your backend moves:
//   Mock (local Mac):  "http://192.168.x.x:8000"  (find IP via System Settings → Wi-Fi → Details)
//   ngrok tunnel:      "https://abc123.ngrok-free.app"
//   Production Vultr:  "https://your-server-ip:8000"
private let kBaseURL = "https://fea6-164-67-70-230.ngrok-free.app"

// TODO: Update with your React dashboard URL
private let kDashboardURL = "https://YOUR_DASHBOARD_URL_HERE"

// MARK: - Upload Response

struct UploadResponse: Decodable {
    let roomId: String
    let status: String
    let accessCode: String?

    enum CodingKeys: String, CodingKey {
        case roomId = "room_id"
        case status
        case accessCode = "access_code"
    }
}

// MARK: - Scan History Models

struct RoomMetadata: Decodable {
    let roomName: String
    let buildingName: String
    let scannedAt: String
    let deviceModel: String?

    enum CodingKeys: String, CodingKey {
        case roomName = "room_name"
        case buildingName = "building_name"
        case scannedAt = "scanned_at"
        case deviceModel = "device_model"
    }
}

struct RoomSummary: Decodable, Identifiable {
    let id: String
    let metadata: RoomMetadata
    let status: String

    enum CodingKeys: String, CodingKey {
        case id = "_id"
        case metadata
        case status
    }
}

struct RoomsListResponse: Decodable {
    let rooms: [RoomSummary]
}

// MARK: - Room Status (for polling)

struct RoomStatus: Decodable {
    let roomId: String
    let status: String
    let accessCode: String?
    let pdfUrl: String?
    let audioUrl: String?
    let narrationText: String?
    let recommendationsPdfUrl: String?
    let recommendationsSummary: String?
    let recommendationsScore: Int?
    let recommendationsCount: Int?
    let statusMapDone: Bool
    let statusNarrationDone: Bool
    let statusRecommendationsDone: Bool

    enum CodingKeys: String, CodingKey {
        case roomId = "room_id"
        case status
        case accessCode = "access_code"
        case pdfUrl = "pdf_url"
        case audioUrl = "audio_url"
        case narrationText = "narration_text"
        case recommendationsPdfUrl = "recommendations_pdf_url"
        case recommendationsSummary = "recommendations_summary"
        case recommendationsScore = "recommendations_score"
        case recommendationsCount = "recommendations_count"
        case statusMapDone = "status_map_done"
        case statusNarrationDone = "status_narration_done"
        case statusRecommendationsDone = "status_recommendations_done"
    }

    var isComplete: Bool {
        statusMapDone && statusNarrationDone && statusRecommendationsDone
    }
}

// MARK: - Voice Session

struct VoiceAgentOverrides: Decodable {
    let prompt: String?
    let firstMessage: String?
    let language: String?

    enum CodingKeys: String, CodingKey {
        case prompt
        case firstMessage = "first_message"
        case language
    }
}

struct VoiceTTSOverrides: Decodable {
    let voiceId: String?

    enum CodingKeys: String, CodingKey {
        case voiceId = "voice_id"
    }
}

struct VoiceSessionResponse: Decodable {
    let conversationToken: String
    let agentId: String
    let agentOverrides: VoiceAgentOverrides
    let ttsOverrides: VoiceTTSOverrides

    enum CodingKeys: String, CodingKey {
        case conversationToken = "conversation_token"
        case agentId = "agent_id"
        case agentOverrides = "agent_overrides"
        case ttsOverrides = "tts_overrides"
    }
}

// MARK: - Errors

enum BackendError: LocalizedError {
    case invalidURL
    case networkError(Error)
    case serverError(statusCode: Int, body: String)
    case decodingError(Error)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Server URL is not configured. Update kBaseURL in BackendClient.swift."
        case .networkError(let e):
            return "Network error: \(e.localizedDescription)"
        case .serverError(let code, let body):
            return "Server error \(code): \(body.prefix(300))"
        case .decodingError(let e):
            return "Unexpected server response: \(e.localizedDescription)"
        }
    }
}

// MARK: - Client

final class BackendClient {
    static let shared = BackendClient()
    private init() {}

    static var dashboardURL: String { kDashboardURL }

    /// Rewrites a localhost URL from the backend to use the public ngrok base URL.
    /// e.g. "http://localhost:8000/files/pdfs/room_x.pdf" → "https://abc.ngrok-free.app/files/pdfs/room_x.pdf"
    static func rewriteFileUrl(_ urlString: String) -> String {
        let localhostPrefixes = [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://0.0.0.0:8000"
        ]
        for prefix in localhostPrefixes {
            if urlString.hasPrefix(prefix) {
                return kBaseURL + urlString.dropFirst(prefix.count)
            }
        }
        return urlString
    }

    private var baseURL: URL {
        get throws {
            guard let url = URL(string: kBaseURL), url.scheme != nil else {
                throw BackendError.invalidURL
            }
            return url
        }
    }

    // MARK: Upload Scan

    func uploadScan(
        scanData: ScanExportData,
        photos: [ARCapturedPhoto],
        roomName: String,
        buildingName: String
    ) async throws -> UploadResponse {
        let url = try baseURL.appendingPathComponent("scan")

        // Serialize ScanExportData → [String: Any] for inclusion in mixed payload
        let scanJSON = try JSONEncoder().encode(scanData)
        guard let scanDict = try JSONSerialization.jsonObject(with: scanJSON) as? [String: Any] else {
            throw BackendError.networkError(
                NSError(domain: "BackendClient", code: -1,
                        userInfo: [NSLocalizedDescriptionKey: "Failed to serialize scan data"])
            )
        }

        // Build photo entries with ARKit pose data
        var photoArray: [[String: Any]] = []
        for photo in photos {
            guard let jpeg = photo.image.jpegData(compressionQuality: 0.7) else { continue }
            
            // Flatten column-major simd_float4x4 into row-major array of 16 floats
            let t = photo.transform
            let transformArray: [Float] = [
                t.columns.0.x, t.columns.1.x, t.columns.2.x, t.columns.3.x,
                t.columns.0.y, t.columns.1.y, t.columns.2.y, t.columns.3.y,
                t.columns.0.z, t.columns.1.z, t.columns.2.z, t.columns.3.z,
                t.columns.0.w, t.columns.1.w, t.columns.2.w, t.columns.3.w
            ]
            
            // Flatten column-major simd_float3x3 into row-major array of 9 floats
            let i = photo.intrinsics
            let intrinsicsArray: [Float] = [
                i.columns.0.x, i.columns.1.x, i.columns.2.x,
                i.columns.0.y, i.columns.1.y, i.columns.2.y,
                i.columns.0.z, i.columns.1.z, i.columns.2.z
            ]
            
            photoArray.append([
                "image_base64": jpeg.base64EncodedString(),
                "camera_transform": transformArray,
                "camera_intrinsics": intrinsicsArray,
                "timestamp": photo.timestamp
            ])
        }

        let payload: [String: Any] = [
            "scan_data": scanDict,
            "photos": photoArray,
            "metadata": [
                "room_name": roomName,
                "building_name": buildingName,
                "scanned_at": ISO8601DateFormatter().string(from: Date()),
                "device_model": deviceModel()
            ]
        ]

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 120  // photos make this a large payload

        let (data, response) = try await perform(request)
        try validate(response, data: data)

        do {
            return try JSONDecoder().decode(UploadResponse.self, from: data)
        } catch {
            throw BackendError.decodingError(error)
        }
    }

    // MARK: Upload Floor Plan

    func uploadFloorPlan(
        image: UIImage,
        buildingName: String,
        locationName: String
    ) async throws -> UploadResponse {
        let url = try baseURL.appendingPathComponent("floorplan")

        // Prefer PNG to preserve crisp linework in floor plans.
        let imageData: Data
        let mimeType: String
        if let pngData = image.pngData() {
            imageData = pngData
            mimeType = "image/png"
        } else if let jpegData = image.jpegData(compressionQuality: 0.9) {
            imageData = jpegData
            mimeType = "image/jpeg"
        } else {
            throw BackendError.networkError(
                NSError(domain: "BackendClient", code: -1,
                        userInfo: [NSLocalizedDescriptionKey: "Failed to encode floor plan image"])
            )
        }

        let imageBase64 = "data:\(mimeType);base64," + imageData.base64EncodedString()

        let payload: [String: Any] = [
            "image_base64": imageBase64,
            "metadata": [
                "building_name": buildingName,
                "location_name": locationName,
            ]
        ]

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 120  // large image payload

        let (data, response) = try await perform(request)
        try validate(response, data: data)

        do {
            return try JSONDecoder().decode(UploadResponse.self, from: data)
        } catch {
            throw BackendError.decodingError(error)
        }
    }

    // MARK: Fetch Rooms

    func fetchRooms() async throws -> [RoomSummary] {
        let url = try baseURL.appendingPathComponent("rooms")
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 30

        let (data, response) = try await perform(request)
        try validate(response, data: data)

        do {
            return try JSONDecoder().decode(RoomsListResponse.self, from: data).rooms
        } catch {
            throw BackendError.decodingError(error)
        }
    }

    // MARK: Trigger Pipeline

    func triggerPipeline(roomId: String) async throws {
        let url = try baseURL.appendingPathComponent("trigger/\(roomId)")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 30

        let (data, response) = try await perform(request)
        try validate(response, data: data)
    }

    // MARK: Poll Room Status

    func pollRoomStatus(roomId: String) async throws -> RoomStatus {
        let url = try baseURL.appendingPathComponent("rooms/\(roomId)/status")
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 15

        let (data, response) = try await perform(request)
        try validate(response, data: data)

        do {
            return try JSONDecoder().decode(RoomStatus.self, from: data)
        } catch {
            throw BackendError.decodingError(error)
        }
    }

    // MARK: Voice Session

    func startVoiceSession(roomId: String) async throws -> VoiceSessionResponse {
        let url = try baseURL.appendingPathComponent("rooms/\(roomId)/voice_session")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 30

        let (data, response) = try await perform(request)
        try validate(response, data: data)

        do {
            return try JSONDecoder().decode(VoiceSessionResponse.self, from: data)
        } catch {
            throw BackendError.decodingError(error)
        }
    }

    // MARK: Helpers

    private func perform(_ request: URLRequest) async throws -> (Data, URLResponse) {
        var mutableRequest = request
        mutableRequest.setValue("true", forHTTPHeaderField: "ngrok-skip-browser-warning")
        do {
            return try await URLSession.shared.data(for: mutableRequest)
        } catch {
            throw BackendError.networkError(error)
        }
    }

    private func validate(_ response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw BackendError.networkError(
                NSError(domain: "BackendClient", code: -1,
                        userInfo: [NSLocalizedDescriptionKey: "Non-HTTP response"])
            )
        }
        guard (200...299).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? "(empty)"
            throw BackendError.serverError(statusCode: http.statusCode, body: body)
        }
    }

    private func deviceModel() -> String {
        "\(UIDevice.current.model) (iOS \(UIDevice.current.systemVersion))"
    }
}
