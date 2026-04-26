import Foundation
import simd

enum RoomVisualizationKind: Equatable {
    case floor
    case wall
    case door(isEntry: Bool)
    case window
    case object(category: String)
}

struct RoomVisualizationElement: Identifiable {
    let id: String
    let kind: RoomVisualizationKind
    let label: String
    let transform: simd_float4x4
    let dimensions: SIMD3<Float>
}

struct RoomPreviewCameraPose {
    let position: SIMD3<Float>
    let target: SIMD3<Float>
}

enum RoomModelVisualization {
    static let floorThickness: Float = 0.02

    static func elements(
        for envelope: RoomModelEnvelope,
        includeFloor: Bool
    ) -> [RoomVisualizationElement] {
        var elements: [RoomVisualizationElement] = []
        let snapshot = envelope.capturedRoomSnapshot

        if includeFloor {
            elements.append(
                RoomVisualizationElement(
                    id: "floor",
                    kind: .floor,
                    label: "Floor",
                    transform: floorTransform(for: snapshot.roomBounds),
                    dimensions: floorDimensions(for: snapshot.roomBounds)
                )
            )
        }

        elements += snapshot.walls.map { surface in
            RoomVisualizationElement(
                id: "wall-\(surface.index)",
                kind: .wall,
                label: RoomLabeling.displayName(for: surface),
                transform: surface.transformMatrix.simd,
                dimensions: RoomGeometry.renderableSurfaceDimensions(
                    from: surface.dimensionsMeters.simd
                )
            )
        }

        elements += snapshot.doors.map { surface in
            let baseLabel = RoomLabeling.displayName(for: surface)
            return RoomVisualizationElement(
                id: "door-\(surface.index)",
                kind: .door(isEntry: surface.index == envelope.entryAnchor.doorIndex),
                label: surface.index == envelope.entryAnchor.doorIndex ? "Entry Door (\(baseLabel))" : baseLabel,
                transform: surface.transformMatrix.simd,
                dimensions: RoomGeometry.renderableSurfaceDimensions(
                    from: surface.dimensionsMeters.simd,
                    thickness: 0.1
                )
            )
        }

        elements += snapshot.windows.map { surface in
            RoomVisualizationElement(
                id: "window-\(surface.index)",
                kind: .window,
                label: RoomLabeling.displayName(for: surface),
                transform: surface.transformMatrix.simd,
                dimensions: RoomGeometry.renderableSurfaceDimensions(
                    from: surface.dimensionsMeters.simd,
                    thickness: 0.06
                )
            )
        }

        elements += snapshot.objects.map { object in
            RoomVisualizationElement(
                id: "object-\(object.index)",
                kind: .object(category: object.category),
                label: RoomLabeling.displayName(for: object),
                transform: object.transformMatrix.simd,
                dimensions: RoomGeometry.renderableObjectDimensions(
                    from: object.dimensionsMeters.simd
                )
            )
        }

        return elements
    }

    static func previewCameraPose(for bounds: RoomSpatialBounds) -> RoomPreviewCameraPose {
        let center = SIMD3<Float>(
            (bounds.minX + bounds.maxX) / 2,
            max((bounds.minY + bounds.maxY) / 2, 0.65),
            (bounds.minZ + bounds.maxZ) / 2
        )
        let spanX = max(bounds.widthMeters, 0.6)
        let spanY = max(bounds.heightMeters, 0.6)
        let spanZ = max(bounds.depthMeters, 0.6)
        let longestSpan = max(spanX, spanZ)
        let distance = longestSpan * 1.3 + spanY * 0.6 + 1.2

        let position = SIMD3<Float>(
            center.x + distance * 0.32,
            center.y + spanY * 0.8 + 0.9,
            center.z + distance
        )

        let target = SIMD3<Float>(
            center.x,
            center.y + max(spanY * 0.1, 0.15),
            center.z
        )

        return RoomPreviewCameraPose(position: position, target: target)
    }

    static func floorDimensions(for bounds: RoomSpatialBounds) -> SIMD3<Float> {
        SIMD3<Float>(
            max(bounds.widthMeters, 0.5),
            floorThickness,
            max(bounds.depthMeters, 0.5)
        )
    }

    static func floorTransform(for bounds: RoomSpatialBounds) -> simd_float4x4 {
        var transform = matrix_identity_float4x4
        transform.columns.3 = SIMD4<Float>(
            (bounds.minX + bounds.maxX) / 2,
            bounds.minY + floorThickness / 2,
            (bounds.minZ + bounds.maxZ) / 2,
            1
        )
        return transform
    }
}
