import Foundation
import GameplayKit
import simd

struct RoomNavMeshConfiguration {
    let cellSize: Float
    let clearance: Float
    let wallThickness: Float
    let doorFrameRadius: Float

    static let `default` = RoomNavMeshConfiguration(
        cellSize: 0.1,
        clearance: 0.45,
        wallThickness: 0.08,
        doorFrameRadius: 0.18
    )
}

final class RoomNavigator {
    private struct CellKey: Hashable {
        let x: Int32
        let z: Int32
    }

    private let snapshot: CapturedRoomSnapshot
    private let roomWorldTransform: simd_float4x4
    private let worldRoomTransform: simd_float4x4
    private let configuration: RoomNavMeshConfiguration
    private let graph: GKGridGraph<GKGridGraphNode>
    private let blockedCells: Set<CellKey>
    private let cellCountX: Int32
    private let cellCountZ: Int32

    init(
        snapshot: CapturedRoomSnapshot,
        roomWorldTransform: simd_float4x4,
        configuration: RoomNavMeshConfiguration = .default
    ) {
        self.snapshot = snapshot
        self.roomWorldTransform = roomWorldTransform
        self.worldRoomTransform = roomWorldTransform.inverse
        self.configuration = configuration

        let width = max(1, Int32(ceil(snapshot.roomBounds.widthMeters / configuration.cellSize)))
        let depth = max(1, Int32(ceil(snapshot.roomBounds.depthMeters / configuration.cellSize)))
        self.cellCountX = width
        self.cellCountZ = depth

        let graph = GKGridGraph(
            fromGridStartingAt: vector_int2(0, 0),
            width: width,
            height: depth,
            diagonalsAllowed: false
        )
        self.graph = graph

        let blocked = Self.computeBlockedCells(
            snapshot: snapshot,
            configuration: configuration,
            cellCountX: width,
            cellCountZ: depth
        )
        self.blockedCells = blocked

        let nodesToRemove: [GKGridGraphNode] = blocked.compactMap { cell in
            graph.node(atGridPosition: vector_int2(cell.x, cell.z))
        }
        graph.remove(nodesToRemove)
    }

    func findPath(from worldStart: SIMD3<Float>, to worldEnd: SIMD3<Float>) -> [SIMD3<Float>] {
        guard
            let startCell = nearestWalkableCell(toRoomPoint: roomPoint(fromWorld: worldStart)),
            let endCell = nearestWalkableCell(toRoomPoint: roomPoint(fromWorld: worldEnd)),
            let startNode = graph.node(atGridPosition: vector_int2(startCell.x, startCell.z)),
            let endNode = graph.node(atGridPosition: vector_int2(endCell.x, endCell.z))
        else {
            return []
        }

        let pathNodes = graph.findPath(from: startNode, to: endNode).compactMap { $0 as? GKGridGraphNode }
        let path = pathNodes.map { worldPoint(for: CellKey(x: $0.gridPosition.x, z: $0.gridPosition.y)) }
        return Self.simplify(path: path)
    }

    func isWalkable(worldPoint: SIMD3<Float>) -> Bool {
        guard let cell = cell(forRoomPoint: roomPoint(fromWorld: worldPoint)) else { return false }
        return !blockedCells.contains(cell)
    }

    func floorWorldY() -> Float {
        let roomFloor = SIMD3<Float>(0, snapshot.roomBounds.minY, 0)
        return worldPoint(fromRoom: roomFloor).y
    }

    private func roomPoint(fromWorld point: SIMD3<Float>) -> SIMD3<Float> {
        let vector = worldRoomTransform * SIMD4<Float>(point.x, point.y, point.z, 1)
        return SIMD3<Float>(vector.x, vector.y, vector.z)
    }

    private func worldPoint(fromRoom point: SIMD3<Float>) -> SIMD3<Float> {
        let vector = roomWorldTransform * SIMD4<Float>(point.x, point.y, point.z, 1)
        return SIMD3<Float>(vector.x, vector.y, vector.z)
    }

    private func worldPoint(for cell: CellKey) -> SIMD3<Float> {
        let roomX = snapshot.roomBounds.minX + (Float(cell.x) + 0.5) * configuration.cellSize
        let roomZ = snapshot.roomBounds.minZ + (Float(cell.z) + 0.5) * configuration.cellSize
        let roomPoint = SIMD3<Float>(roomX, snapshot.roomBounds.minY + 0.01, roomZ)
        return worldPoint(fromRoom: roomPoint)
    }

    private func cell(forRoomPoint point: SIMD3<Float>) -> CellKey? {
        let xFloat = floor((point.x - snapshot.roomBounds.minX) / configuration.cellSize)
        let zFloat = floor((point.z - snapshot.roomBounds.minZ) / configuration.cellSize)
        guard xFloat.isFinite, zFloat.isFinite else { return nil }
        let x = Int32(xFloat)
        let z = Int32(zFloat)
        guard x >= 0, z >= 0, x < cellCountX, z < cellCountZ else { return nil }
        return CellKey(x: x, z: z)
    }

    private func nearestWalkableCell(toRoomPoint point: SIMD3<Float>) -> CellKey? {
        guard let origin = cell(forRoomPoint: point) else { return nil }
        if !blockedCells.contains(origin) {
            return origin
        }

        let maxRadius = max(cellCountX, cellCountZ)
        for radius in 1...maxRadius {
            for x in max(0, origin.x - radius)...min(cellCountX - 1, origin.x + radius) {
                for z in max(0, origin.z - radius)...min(cellCountZ - 1, origin.z + radius) {
                    let candidate = CellKey(x: x, z: z)
                    if !blockedCells.contains(candidate) {
                        return candidate
                    }
                }
            }
        }

        return nil
    }

    private static func computeBlockedCells(
        snapshot: CapturedRoomSnapshot,
        configuration: RoomNavMeshConfiguration,
        cellCountX: Int32,
        cellCountZ: Int32
    ) -> Set<CellKey> {
        var blocked: Set<CellKey> = []

        let wallRects = snapshot.walls.map { surface in
            OrientedRect(
                transform: surface.transformMatrix.simd,
                dimensions: RoomGeometry.renderableSurfaceDimensions(
                    from: surface.dimensionsMeters.simd,
                    thickness: configuration.wallThickness
                )
            )
        }

        let objectRects = snapshot.objects.map { object in
            OrientedRect(
                transform: object.transformMatrix.simd,
                dimensions: SIMD3<Float>(
                    object.dimensionsMeters.simd.x + configuration.clearance * 2,
                    object.dimensionsMeters.simd.y,
                    object.dimensionsMeters.simd.z + configuration.clearance * 2
                )
            )
        }

        let doorFrameCenters = snapshot.doors.flatMap { surface -> [SIMD2<Float>] in
            let endpoints = RoomGeometry.surfaceEndpoints(
                transform: surface.transformMatrix.simd,
                width: surface.dimensionsMeters.simd.x
            )
            return [
                SIMD2<Float>(endpoints.0.x, endpoints.0.z),
                SIMD2<Float>(endpoints.1.x, endpoints.1.z)
            ]
        }

        for x in 0..<cellCountX {
            for z in 0..<cellCountZ {
                let point = SIMD2<Float>(
                    snapshot.roomBounds.minX + (Float(x) + 0.5) * configuration.cellSize,
                    snapshot.roomBounds.minZ + (Float(z) + 0.5) * configuration.cellSize
                )

                let blockedByWall = wallRects.contains { $0.contains(point) }
                let blockedByObject = objectRects.contains { $0.contains(point) }
                let blockedByDoorFrame = doorFrameCenters.contains { simd_distance($0, point) <= configuration.doorFrameRadius }

                if blockedByWall || blockedByObject || blockedByDoorFrame {
                    blocked.insert(CellKey(x: x, z: z))
                }
            }
        }

        return blocked
    }

    private static func simplify(path: [SIMD3<Float>]) -> [SIMD3<Float>] {
        guard path.count > 2 else { return path }

        var result: [SIMD3<Float>] = [path[0]]

        for index in 1..<(path.count - 1) {
            let previous = result.last ?? path[index - 1]
            let current = path[index]
            let next = path[index + 1]

            let incoming = simd_normalize(SIMD2<Float>(current.x - previous.x, current.z - previous.z))
            let outgoing = simd_normalize(SIMD2<Float>(next.x - current.x, next.z - current.z))

            if simd_length(incoming - outgoing) > 0.001 {
                result.append(current)
            }
        }

        result.append(path[path.count - 1])
        return result
    }
}

private struct OrientedRect {
    let center: SIMD2<Float>
    let xAxis: SIMD2<Float>
    let zAxis: SIMD2<Float>
    let halfWidth: Float
    let halfDepth: Float

    init(transform: simd_float4x4, dimensions: SIMD3<Float>) {
        let translation = RoomGeometry.translation(of: transform)
        let xVector = RoomGeometry.horizontalAxis(of: transform, column: 0)
        let zVector = RoomGeometry.horizontalAxis(of: transform, column: 2)
        self.center = SIMD2<Float>(translation.x, translation.z)
        self.xAxis = simd_normalize(SIMD2<Float>(xVector.x, xVector.z))
        self.zAxis = simd_normalize(SIMD2<Float>(zVector.x, zVector.z))
        self.halfWidth = max(dimensions.x / 2, RoomGeometry.minimumRenderableExtent)
        self.halfDepth = max(dimensions.z / 2, RoomGeometry.minimumRenderableExtent)
    }

    func contains(_ point: SIMD2<Float>) -> Bool {
        let delta = point - center
        let localX = simd_dot(delta, xAxis)
        let localZ = simd_dot(delta, zAxis)
        return abs(localX) <= halfWidth && abs(localZ) <= halfDepth
    }
}
