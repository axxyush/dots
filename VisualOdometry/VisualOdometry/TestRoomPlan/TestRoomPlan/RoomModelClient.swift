import Foundation
import UIKit
import simd

final class RoomModelClient {
    static let shared = RoomModelClient()
    private init() {}

    private var baseURL: URL {
        get throws {
            guard let url = URL(string: BackendEnvironment.baseURLString), url.scheme != nil else {
                throw BackendError.invalidURL
            }
            return url
        }
    }

    func uploadRoomModel(_ envelope: RoomModelEnvelopeUploadRequest) async throws -> RoomModelUploadResponse {
        let url = try baseURL.appendingPathComponent("room-models")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        request.httpBody = try encoder.encode(envelope)
        request.timeoutInterval = 60

        let (data, response) = try await URLSession.shared.data(for: request)
        try validate(response: response, data: data)

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(RoomModelUploadResponse.self, from: data)
    }

    func fetchRoomModel(roomID: String) async throws -> RoomModelEnvelope {
        let url = try baseURL.appendingPathComponent("room-models/\(roomID)")
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 30

        let (data, response) = try await URLSession.shared.data(for: request)
        try validate(response: response, data: data)

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let wireEnvelope = try decoder.decode(RoomModelEnvelopeResponse.self, from: data)
        return wireEnvelope.normalizedEnvelope(fallbackRoomID: roomID)
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw BackendError.networkError(
                NSError(
                    domain: "RoomModelClient",
                    code: -1,
                    userInfo: [NSLocalizedDescriptionKey: "Non-HTTP response"]
                )
            )
        }

        guard (200...299).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? "(empty)"
            throw BackendError.serverError(statusCode: http.statusCode, body: body)
        }
    }
}

private struct RoomModelEnvelopeResponse: Decodable {
    let roomModelEnvelope: RoomModelEnvelope?
    let directEnvelope: RoomModelEnvelope?

    enum CodingKeys: String, CodingKey {
        case roomModelEnvelope
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if container.contains(.roomModelEnvelope) {
            roomModelEnvelope = try container.decode(RoomModelEnvelope.self, forKey: .roomModelEnvelope)
            directEnvelope = nil
        } else {
            roomModelEnvelope = nil
            directEnvelope = try RoomModelEnvelope(from: decoder)
        }
    }

    func normalizedEnvelope(fallbackRoomID: String) -> RoomModelEnvelope {
        let envelope = roomModelEnvelope ?? directEnvelope ?? RoomModelEnvelope(
            schemaVersion: RoomModelExporter.schemaVersion,
            roomID: fallbackRoomID,
            marker: MarkerSnapshot(
                templateVersion: RoomModelExporter.markerTemplateVersion,
                physicalWidthMeters: RoomModelExporter.markerPhysicalWidthMeters,
                uuid: fallbackRoomID
            ),
            entryAnchor: EntryAnchorSnapshot(
                doorIndex: 0,
                anchorType: "door",
                anchorIndex: 0,
                transformMatrix: TransformMatrixData(matrix_identity_float4x4),
                positionMeters: Vector3Data(x: 0, y: 0, z: 0)
            ),
            capturedRoomSnapshot: CapturedRoomSnapshot(
                originTransform: TransformMatrixData(matrix_identity_float4x4),
                roomBounds: RoomSpatialBounds(minX: 0, maxX: 0, minY: 0, maxY: 0, minZ: 0, maxZ: 0),
                walls: [],
                doors: [],
                windows: [],
                objects: []
            )
        )

        if envelope.roomID == nil || envelope.marker.uuid == nil {
            return envelope.withRoomID(fallbackRoomID)
        }
        return envelope
    }
}
