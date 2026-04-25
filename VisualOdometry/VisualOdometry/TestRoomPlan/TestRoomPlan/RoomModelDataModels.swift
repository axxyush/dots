import Foundation
import simd

struct RoomSpatialBounds: Codable, Equatable {
    let minX: Float
    let maxX: Float
    let minY: Float
    let maxY: Float
    let minZ: Float
    let maxZ: Float

    var widthMeters: Float { maxX - minX }
    var depthMeters: Float { maxZ - minZ }
    var heightMeters: Float { maxY - minY }
}

struct Vector3Data: Codable, Equatable {
    let x: Float
    let y: Float
    let z: Float

    init(x: Float, y: Float, z: Float) {
        self.x = x
        self.y = y
        self.z = z
    }

    init(_ vector: SIMD3<Float>) {
        self.init(x: vector.x, y: vector.y, z: vector.z)
    }

    var simd: SIMD3<Float> {
        SIMD3<Float>(x, y, z)
    }
}

struct TransformMatrixData: Codable, Equatable {
    let elements: [Float]

    init(_ matrix: simd_float4x4) {
        self.elements = RoomGeometry.columnMajorArray(from: matrix)
    }

    init(elements: [Float]) {
        self.elements = elements
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let elements = try container.decode([Float].self)
        guard elements.count == 16 else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Transform matrices must contain 16 column-major Float values."
            )
        }
        self.elements = elements
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(elements)
    }

    var simd: simd_float4x4 {
        RoomGeometry.matrix(fromColumnMajor: elements)
    }
}

struct MarkerSnapshot: Codable, Equatable {
    let templateVersion: String
    let physicalWidthMeters: Float
    let uuid: String?
}

struct EntryAnchorSnapshot: Codable, Equatable {
    let doorIndex: Int
    let transformMatrix: TransformMatrixData
    let positionMeters: Vector3Data
}

struct SurfaceSnapshot: Codable, Equatable, Identifiable {
    let index: Int
    let category: String
    let dimensionsMeters: Vector3Data
    let transformMatrix: TransformMatrixData

    var id: Int { index }
}

struct ObjectSnapshot: Codable, Equatable, Identifiable {
    let index: Int
    let category: String
    let dimensionsMeters: Vector3Data
    let transformMatrix: TransformMatrixData
    let confidence: String?

    var id: Int { index }
}

struct CapturedRoomSnapshot: Codable, Equatable {
    let originTransform: TransformMatrixData
    let roomBounds: RoomSpatialBounds
    let walls: [SurfaceSnapshot]
    let doors: [SurfaceSnapshot]
    let windows: [SurfaceSnapshot]
    let objects: [ObjectSnapshot]
}

struct RoomModelEnvelope: Codable, Equatable {
    let schemaVersion: String
    let roomID: String?
    let marker: MarkerSnapshot
    let entryAnchor: EntryAnchorSnapshot
    let capturedRoomSnapshot: CapturedRoomSnapshot

    enum CodingKeys: String, CodingKey {
        case schemaVersion
        case roomID = "roomId"
        case marker
        case entryAnchor
        case capturedRoomSnapshot
    }

    func withRoomID(_ roomID: String) -> RoomModelEnvelope {
        RoomModelEnvelope(
            schemaVersion: schemaVersion,
            roomID: roomID,
            marker: MarkerSnapshot(
                templateVersion: marker.templateVersion,
                physicalWidthMeters: marker.physicalWidthMeters,
                uuid: roomID
            ),
            entryAnchor: entryAnchor,
            capturedRoomSnapshot: capturedRoomSnapshot
        )
    }
}

struct RoomModelEnvelopeUploadRequest: Codable, Equatable {
    let roomModelEnvelope: RoomModelEnvelope
}

struct RoomModelUploadResponse: Decodable, Equatable {
    let roomID: String

    enum CodingKeys: String, CodingKey {
        case roomID = "roomId"
    }
}
