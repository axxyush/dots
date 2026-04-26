import Foundation
import RoomPlan
import simd

enum RoomModelExporter {
    static let schemaVersion = "1.0"
    static let markerTemplateVersion = "dots_entry_v1"
    static let markerPhysicalWidthMeters: Float = 0.10

    static func makeUploadRequest(
        room: CapturedRoom,
        selectedAnchor: SelectedAnchor,
        doorLabelOverrides: [Int: String] = [:],
        objectLabelOverrides: [Int: String] = [:]
    ) -> RoomModelEnvelopeUploadRequest {
        RoomModelEnvelopeUploadRequest(
            roomModelEnvelope: makeEnvelope(
                room: room,
                roomID: nil,
                selectedAnchor: selectedAnchor,
                doorLabelOverrides: doorLabelOverrides,
                objectLabelOverrides: objectLabelOverrides
            )
        )
    }

    static func makeEnvelope(
        room: CapturedRoom,
        roomID: String?,
        selectedAnchor: SelectedAnchor,
        doorLabelOverrides: [Int: String] = [:],
        objectLabelOverrides: [Int: String] = [:]
    ) -> RoomModelEnvelope {
        let walls = room.walls.enumerated().map {
            surfaceSnapshot(index: $0.offset, category: "wall", surface: $0.element)
        }
        let doors = room.doors.enumerated().map {
            surfaceSnapshot(
                index: $0.offset,
                category: "door",
                surface: $0.element,
                labelOverride: doorLabelOverrides[$0.offset]
            )
        }
        let windows = room.windows.enumerated().map {
            surfaceSnapshot(index: $0.offset, category: "window", surface: $0.element)
        }
        let objects = room.objects.enumerated().map {
            objectSnapshot(
                index: $0.offset,
                object: $0.element,
                labelOverride: objectLabelOverrides[$0.offset]
            )
        }

        let wallBoxes = room.walls.map {
            RoomGeometry.orientedBoxCorners(
                transform: $0.transform,
                dimensions: SIMD3<Float>($0.dimensions.x, $0.dimensions.y, 0)
            )
        }
        let doorBoxes = room.doors.map {
            RoomGeometry.orientedBoxCorners(
                transform: $0.transform,
                dimensions: SIMD3<Float>($0.dimensions.x, $0.dimensions.y, 0)
            )
        }
        let windowBoxes = room.windows.map {
            RoomGeometry.orientedBoxCorners(
                transform: $0.transform,
                dimensions: SIMD3<Float>($0.dimensions.x, $0.dimensions.y, 0)
            )
        }
        let objectBoxes = room.objects.map {
            RoomGeometry.orientedBoxCorners(
                transform: $0.transform,
                dimensions: $0.dimensions
            )
        }
        let roomBounds = RoomGeometry.bounds(
            boxCorners: wallBoxes + doorBoxes + windowBoxes + objectBoxes,
            includeOrigin: true,
            padding: 0
        )

        let anchorTransform: simd_float4x4
        let doorIndex: Int?
        let anchorIndex: Int?
        let anchorType: String?
        
        switch selectedAnchor {
        case .door(let index):
            anchorTransform = room.doors[index].transform
            doorIndex = index
            anchorIndex = index
            anchorType = "door"
        case .object(let index):
            anchorTransform = room.objects[index].transform
            doorIndex = nil
            anchorIndex = index
            anchorType = "object"
        }

        let entryAnchor = EntryAnchorSnapshot(
            doorIndex: doorIndex,
            anchorType: anchorType,
            anchorIndex: anchorIndex,
            transformMatrix: TransformMatrixData(anchorTransform),
            positionMeters: Vector3Data(RoomGeometry.translation(of: anchorTransform))
        )

        let snapshot = CapturedRoomSnapshot(
            originTransform: TransformMatrixData(matrix_identity_float4x4),
            roomBounds: roomBounds,
            walls: walls,
            doors: doors,
            windows: windows,
            objects: objects
        )

        return RoomModelEnvelope(
            schemaVersion: schemaVersion,
            roomID: roomID,
            marker: MarkerSnapshot(
                templateVersion: markerTemplateVersion,
                physicalWidthMeters: markerPhysicalWidthMeters,
                uuid: roomID
            ),
            entryAnchor: entryAnchor,
            capturedRoomSnapshot: snapshot
        )
    }

    static func saveJSON(_ envelope: RoomModelEnvelope) -> URL? {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.keyEncodingStrategy = .convertToSnakeCase

        guard let jsonData = try? encoder.encode(RoomModelEnvelopeUploadRequest(roomModelEnvelope: envelope)) else {
            return nil
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let filename = "room_model_\(formatter.string(from: Date())).json"
        let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let url = directory.appendingPathComponent(filename)

        do {
            try jsonData.write(to: url)
            return url
        } catch {
            return nil
        }
    }

    private static func surfaceSnapshot(
        index: Int,
        category: String,
        surface: CapturedRoom.Surface,
        labelOverride: String? = nil
    ) -> SurfaceSnapshot {
        return SurfaceSnapshot(
            index: index,
            category: category,
            label: RoomLabeling.sanitizedOverride(labelOverride) ?? RoomLabeling.defaultSurfaceLabel(category: category, index: index),
            dimensionsMeters: Vector3Data(x: surface.dimensions.x, y: surface.dimensions.y, z: 0),
            transformMatrix: TransformMatrixData(surface.transform)
        )
    }

    private static func objectSnapshot(
        index: Int,
        object: CapturedRoom.Object,
        labelOverride: String? = nil
    ) -> ObjectSnapshot {
        let categoryName = RoomExporter.objectCategoryName(object.category)
        return ObjectSnapshot(
            index: index,
            category: categoryName,
            label: RoomLabeling.sanitizedOverride(labelOverride) ?? RoomLabeling.defaultObjectLabel(category: categoryName, index: index),
            dimensionsMeters: Vector3Data(object.dimensions),
            transformMatrix: TransformMatrixData(object.transform),
            confidence: RoomExporter.confidenceName(object.confidence)
        )
    }
}
