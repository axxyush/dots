import Foundation
import simd

enum RoomGeometry {
    static let defaultSurfaceThickness: Float = 0.08
    static let minimumRenderableExtent: Float = 0.02

    static func columnMajorArray(from matrix: simd_float4x4) -> [Float] {
        [
            matrix.columns.0.x, matrix.columns.0.y, matrix.columns.0.z, matrix.columns.0.w,
            matrix.columns.1.x, matrix.columns.1.y, matrix.columns.1.z, matrix.columns.1.w,
            matrix.columns.2.x, matrix.columns.2.y, matrix.columns.2.z, matrix.columns.2.w,
            matrix.columns.3.x, matrix.columns.3.y, matrix.columns.3.z, matrix.columns.3.w
        ]
    }

    static func matrix(fromColumnMajor elements: [Float]) -> simd_float4x4 {
        guard elements.count == 16 else { return matrix_identity_float4x4 }
        return simd_float4x4(
            SIMD4<Float>(elements[0], elements[1], elements[2], elements[3]),
            SIMD4<Float>(elements[4], elements[5], elements[6], elements[7]),
            SIMD4<Float>(elements[8], elements[9], elements[10], elements[11]),
            SIMD4<Float>(elements[12], elements[13], elements[14], elements[15])
        )
    }

    static func translation(of transform: simd_float4x4) -> SIMD3<Float> {
        SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
    }

    static func axis(of transform: simd_float4x4, column: Int) -> SIMD3<Float> {
        let sourceColumn: SIMD4<Float>
        let fallback: SIMD3<Float>

        switch column {
        case 0:
            sourceColumn = transform.columns.0
            fallback = SIMD3<Float>(1, 0, 0)
        case 1:
            sourceColumn = transform.columns.1
            fallback = SIMD3<Float>(0, 1, 0)
        case 2:
            sourceColumn = transform.columns.2
            fallback = SIMD3<Float>(0, 0, 1)
        default:
            sourceColumn = transform.columns.0
            fallback = SIMD3<Float>(1, 0, 0)
        }

        let axis = SIMD3<Float>(sourceColumn.x, sourceColumn.y, sourceColumn.z)
        let length = simd_length(axis)
        if length > 0.0001 {
            return axis / length
        }

        return fallback
    }

    static func horizontalAxis(of transform: simd_float4x4, column: Int) -> SIMD3<Float> {
        let sourceAxis = axis(of: transform, column: column)
        let axis = SIMD3<Float>(sourceAxis.x, 0, sourceAxis.z)
        let length = simd_length(axis)
        if length > 0.0001 {
            return axis / length
        }

        switch column {
        case 2:
            return SIMD3<Float>(0, 0, 1)
        default:
            return SIMD3<Float>(1, 0, 0)
        }
    }

    static func surfaceEndpoints(transform: simd_float4x4, width: Float) -> (SIMD3<Float>, SIMD3<Float>) {
        let center = translation(of: transform)
        let direction = horizontalAxis(of: transform, column: 0)
        let halfWidth = width / 2
        return (center + direction * halfWidth, center - direction * halfWidth)
    }

    static func orientedFootprint(transform: simd_float4x4, dimensions: SIMD3<Float>) -> [SIMD2<Float>] {
        let center = translation(of: transform)
        let xAxis = horizontalAxis(of: transform, column: 0)
        let zAxis = horizontalAxis(of: transform, column: 2)
        let halfWidth = dimensions.x / 2
        let halfDepth = dimensions.z / 2

        let corners3D = [
            center + xAxis * halfWidth + zAxis * halfDepth,
            center + xAxis * halfWidth - zAxis * halfDepth,
            center - xAxis * halfWidth - zAxis * halfDepth,
            center - xAxis * halfWidth + zAxis * halfDepth
        ]

        return corners3D.map { SIMD2<Float>($0.x, $0.z) }
    }

    static func orientedBoxCorners(transform: simd_float4x4, dimensions: SIMD3<Float>) -> [SIMD3<Float>] {
        let center = translation(of: transform)
        let xAxis = axis(of: transform, column: 0)
        let yAxis = axis(of: transform, column: 1)
        let zAxis = axis(of: transform, column: 2)
        let halfWidth = dimensions.x / 2
        let halfHeight = dimensions.y / 2
        let halfDepth = dimensions.z / 2

        return [
            center + xAxis * halfWidth + yAxis * halfHeight + zAxis * halfDepth,
            center + xAxis * halfWidth + yAxis * halfHeight - zAxis * halfDepth,
            center + xAxis * halfWidth - yAxis * halfHeight + zAxis * halfDepth,
            center + xAxis * halfWidth - yAxis * halfHeight - zAxis * halfDepth,
            center - xAxis * halfWidth + yAxis * halfHeight + zAxis * halfDepth,
            center - xAxis * halfWidth + yAxis * halfHeight - zAxis * halfDepth,
            center - xAxis * halfWidth - yAxis * halfHeight + zAxis * halfDepth,
            center - xAxis * halfWidth - yAxis * halfHeight - zAxis * halfDepth
        ]
    }

    static func renderableSurfaceDimensions(
        from dimensions: SIMD3<Float>,
        thickness: Float = defaultSurfaceThickness
    ) -> SIMD3<Float> {
        SIMD3<Float>(
            max(dimensions.x, minimumRenderableExtent),
            max(dimensions.y, minimumRenderableExtent),
            max(dimensions.z, thickness)
        )
    }

    static func renderableObjectDimensions(from dimensions: SIMD3<Float>) -> SIMD3<Float> {
        SIMD3<Float>(
            max(dimensions.x, minimumRenderableExtent),
            max(dimensions.y, minimumRenderableExtent),
            max(dimensions.z, minimumRenderableExtent)
        )
    }

    static func bounds(
        boxCorners: [[SIMD3<Float>]],
        includeOrigin: Bool,
        padding: Float
    ) -> RoomSpatialBounds {
        var minX: Float = .infinity
        var maxX: Float = -.infinity
        var minY: Float = .infinity
        var maxY: Float = -.infinity
        var minZ: Float = .infinity
        var maxZ: Float = -.infinity

        func include(_ point: SIMD3<Float>) {
            minX = min(minX, point.x)
            maxX = max(maxX, point.x)
            minY = min(minY, point.y)
            maxY = max(maxY, point.y)
            minZ = min(minZ, point.z)
            maxZ = max(maxZ, point.z)
        }

        for corners in boxCorners {
            for corner in corners {
                include(corner)
            }
        }

        if includeOrigin {
            include(SIMD3<Float>(0, 0, 0))
        }

        if !minX.isFinite {
            minX = 0
            maxX = 0
            minY = 0
            maxY = 0
            minZ = 0
            maxZ = 0
        }

        return RoomSpatialBounds(
            minX: minX - padding,
            maxX: maxX + padding,
            minY: minY - padding,
            maxY: maxY + padding,
            minZ: minZ - padding,
            maxZ: maxZ + padding
        )
    }

    static func bounds(
        surfaces: [(SIMD3<Float>, SIMD3<Float>)],
        objectFootprints: [[SIMD2<Float>]],
        includeOrigin: Bool,
        padding: Float
    ) -> RoomSpatialBounds {
        var minX: Float = .infinity
        var maxX: Float = -.infinity
        var minY: Float = .infinity
        var maxY: Float = -.infinity
        var minZ: Float = .infinity
        var maxZ: Float = -.infinity

        func include(_ point: SIMD3<Float>) {
            minX = min(minX, point.x)
            maxX = max(maxX, point.x)
            minY = min(minY, point.y)
            maxY = max(maxY, point.y)
            minZ = min(minZ, point.z)
            maxZ = max(maxZ, point.z)
        }

        func include(_ point: SIMD2<Float>) {
            minX = min(minX, point.x)
            maxX = max(maxX, point.x)
            minZ = min(minZ, point.y)
            maxZ = max(maxZ, point.y)
        }

        for surface in surfaces {
            include(surface.0)
            include(surface.1)
        }

        for footprint in objectFootprints {
            for corner in footprint {
                include(corner)
            }
        }

        if includeOrigin {
            include(SIMD3<Float>(0, 0, 0))
        }

        if !minX.isFinite {
            minX = 0
            maxX = 0
            minY = 0
            maxY = 0
            minZ = 0
            maxZ = 0
        }

        return RoomSpatialBounds(
            minX: minX - padding,
            maxX: maxX + padding,
            minY: minY,
            maxY: maxY,
            minZ: minZ - padding,
            maxZ: maxZ + padding
        )
    }
}
