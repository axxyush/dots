import Foundation
import simd

struct DestinationCandidate: Identifiable, Equatable {
    let id: UUID
    let name: String
    let roomPosition: SIMD3<Float>
    let worldPosition: SIMD3<Float>
}

final class RoomDestinationResolver {
    private let envelope: RoomModelEnvelope
    private let roomWorldTransform: simd_float4x4
    private(set) var currentUserWorldPosition: SIMD3<Float>?
    private(set) var candidates: [DestinationCandidate]

    init(envelope: RoomModelEnvelope, roomWorldTransform: simd_float4x4) {
        self.envelope = envelope
        self.roomWorldTransform = roomWorldTransform
        self.candidates = Self.buildCandidates(
            snapshot: envelope.capturedRoomSnapshot,
            entryAnchor: envelope.entryAnchor,
            roomWorldTransform: roomWorldTransform
        )
    }

    var destinationNames: [String] {
        let preferredOrder = ["Bathroom", "Seating Area", "Bed", "Exit"]
        let unique = Array(Set(candidates.map(\.name)))
        return unique.sorted { left, right in
            let leftIndex = preferredOrder.firstIndex(of: left) ?? preferredOrder.count
            let rightIndex = preferredOrder.firstIndex(of: right) ?? preferredOrder.count
            if leftIndex == rightIndex {
                return left < right
            }
            return leftIndex < rightIndex
        }
    }

    func updateUserWorldPosition(_ position: SIMD3<Float>) {
        currentUserWorldPosition = position
    }

    func resolveDestination(_ query: String) -> SIMD3<Float>? {
        resolveCandidate(query)?.worldPosition
    }

    func resolveCandidate(_ query: String) -> DestinationCandidate? {
        let normalized = query
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()

        guard !normalized.isEmpty else { return nil }

        let matchingNames = destinationNames.filter { name in
            let canonical = name.lowercased()
            if canonical.contains(normalized) || normalized.contains(canonical) {
                return true
            }

            switch canonical {
            case "bathroom":
                return ["bathroom", "restroom", "toilet", "sink", "bath", "washroom"].contains(where: normalized.contains)
            case "seating area":
                return ["seat", "seating", "chair", "table", "lounge"].contains(where: normalized.contains)
            case "bed":
                return ["bed", "sleep"].contains(where: normalized.contains)
            case "exit":
                return ["exit", "door", "entrance", "entry"].contains(where: normalized.contains)
            default:
                return false
            }
        }

        let pool: [DestinationCandidate]
        if matchingNames.isEmpty {
            pool = candidates
        } else {
            pool = candidates.filter { matchingNames.contains($0.name) }
        }

        guard !pool.isEmpty else { return nil }

        if let userPosition = currentUserWorldPosition {
            return pool.min { simd_distance($0.worldPosition, userPosition) < simd_distance($1.worldPosition, userPosition) }
        }

        return pool.first
    }

    private static func buildCandidates(
        snapshot: CapturedRoomSnapshot,
        entryAnchor: EntryAnchorSnapshot,
        roomWorldTransform: simd_float4x4
    ) -> [DestinationCandidate] {
        let objects = snapshot.objects.map { object -> (category: String, roomPosition: SIMD3<Float>) in
            (object.category.lowercased(), RoomGeometry.translation(of: object.transformMatrix.simd))
        }

        var results: [DestinationCandidate] = []

        let bathroomObjects = objects.filter { ["toilet", "bathtub", "sink"].contains($0.category) }
        for cluster in clustered(items: bathroomObjects, threshold: 1.8) where !cluster.isEmpty {
            let center = averagePosition(of: cluster.map(\.roomPosition))
            results.append(
                DestinationCandidate(
                    id: UUID(),
                    name: "Bathroom",
                    roomPosition: center,
                    worldPosition: transform(center, by: roomWorldTransform)
                )
            )
        }

        let tables = objects.filter { $0.category == "table" }
        let chairs = objects.filter { $0.category == "chair" }
        for table in tables {
            let nearbyChairs = chairs.filter { simd_distance($0.roomPosition, table.roomPosition) <= 1.8 }
            guard !nearbyChairs.isEmpty else { continue }
            let center = averagePosition(of: [table.roomPosition] + nearbyChairs.map(\.roomPosition))
            results.append(
                DestinationCandidate(
                    id: UUID(),
                    name: "Seating Area",
                    roomPosition: center,
                    worldPosition: transform(center, by: roomWorldTransform)
                )
            )
        }

        for bed in objects where bed.category == "bed" {
            results.append(
                DestinationCandidate(
                    id: UUID(),
                    name: "Bed",
                    roomPosition: bed.roomPosition,
                    worldPosition: transform(bed.roomPosition, by: roomWorldTransform)
                )
            )
        }

        let exitPosition = entryAnchor.positionMeters.simd
        results.append(
            DestinationCandidate(
                id: UUID(),
                name: "Exit",
                roomPosition: exitPosition,
                worldPosition: transform(exitPosition, by: roomWorldTransform)
            )
        )

        return results
    }

    private static func transform(_ point: SIMD3<Float>, by matrix: simd_float4x4) -> SIMD3<Float> {
        let vector = matrix * SIMD4<Float>(point.x, point.y, point.z, 1)
        return SIMD3<Float>(vector.x, vector.y, vector.z)
    }

    private static func averagePosition(of points: [SIMD3<Float>]) -> SIMD3<Float> {
        guard !points.isEmpty else { return .zero }
        let total = points.reduce(SIMD3<Float>.zero, +)
        return total / Float(points.count)
    }

    private static func clustered(
        items: [(category: String, roomPosition: SIMD3<Float>)],
        threshold: Float
    ) -> [[(category: String, roomPosition: SIMD3<Float>)]] {
        guard !items.isEmpty else { return [] }

        var remaining = Set(items.indices)
        var clusters: [[(category: String, roomPosition: SIMD3<Float>)]] = []

        while let seed = remaining.first {
            var queue = [seed]
            var cluster: [Int] = []
            remaining.remove(seed)

            while let current = queue.popLast() {
                cluster.append(current)
                let currentPosition = items[current].roomPosition

                let neighbors = remaining.filter { candidate in
                    simd_distance(currentPosition, items[candidate].roomPosition) <= threshold
                }

                for neighbor in neighbors {
                    remaining.remove(neighbor)
                    queue.append(neighbor)
                }
            }

            clusters.append(cluster.map { items[$0] })
        }

        return clusters
    }
}
