import Foundation
import simd

enum DestinationKind: Equatable {
    case zone
    case door(index: Int, isEntry: Bool)
    case object(index: Int)
}

struct DestinationCandidate: Identifiable, Equatable {
    let id: UUID
    let name: String
    let aliases: [String]
    let kind: DestinationKind
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
        var seen: Set<String> = []
        return orderedCandidates.compactMap { candidate in
            guard seen.insert(candidate.name).inserted else { return nil }
            return candidate.name
        }
    }

    func updateUserWorldPosition(_ position: SIMD3<Float>) {
        currentUserWorldPosition = position
    }

    func resolveDestination(_ query: String) -> SIMD3<Float>? {
        resolveCandidate(query)?.worldPosition
    }

    func resolveCandidate(_ query: String) -> DestinationCandidate? {
        let normalized = Self.normalizedSearchKey(query)
        guard !normalized.isEmpty else { return nil }

        if let directDoor = resolveDoorCandidate(query, normalized: normalized) {
            return directDoor
        }

        if Self.isExitQuery(normalized), let entryDoor = orderedCandidates.first(where: isEntryDoor(_:)) {
            return entryDoor
        }

        let pool = orderedCandidates.filter { candidate in
            candidate.aliases.contains { alias in
                let normalizedAlias = Self.normalizedSearchKey(alias)
                return normalizedAlias.contains(normalized) || normalized.contains(normalizedAlias)
            }
        }

        if !pool.isEmpty {
            return preferredCandidate(from: pool)
        }

        let fallbackPool = orderedCandidates.filter { candidate in
            let candidateWords = candidate.name.lowercased().split(separator: " ").map(String.init)
            return candidateWords.contains { normalized.contains(Self.normalizedSearchKey($0)) }
        }

        guard !fallbackPool.isEmpty else { return nil }
        return preferredCandidate(from: fallbackPool)
    }

    private var orderedCandidates: [DestinationCandidate] {
        candidates.sorted { left, right in
            let leftRank = Self.sortRank(for: left)
            let rightRank = Self.sortRank(for: right)
            if leftRank != rightRank {
                return leftRank < rightRank
            }
            return left.name.localizedCaseInsensitiveCompare(right.name) == .orderedAscending
        }
    }

    private func preferredCandidate(from pool: [DestinationCandidate]) -> DestinationCandidate? {
        if let userPosition = currentUserWorldPosition {
            return pool.min {
                simd_distance($0.worldPosition, userPosition) < simd_distance($1.worldPosition, userPosition)
            }
        }
        return pool.sorted {
            let leftRank = Self.sortRank(for: $0)
            let rightRank = Self.sortRank(for: $1)
            if leftRank != rightRank {
                return leftRank < rightRank
            }
            return $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }.first
    }

    private func resolveDoorCandidate(_ rawQuery: String, normalized: String) -> DestinationCandidate? {
        guard normalized.contains("door") else { return nil }

        let matches = orderedCandidates.filter { candidate in
            if case .door = candidate.kind {
                let candidateKey = Self.normalizedSearchKey(candidate.name)
                return normalized.contains(candidateKey) || candidate.aliases.contains {
                    normalized.contains(Self.normalizedSearchKey($0))
                }
            }
            return false
        }

        if !matches.isEmpty {
            return preferredCandidate(from: matches)
        }

        let digits = rawQuery.filter(\.isNumber)
        if let number = Int(digits), number > 0 {
            return orderedCandidates.first { candidate in
                if case .door(let index, _) = candidate.kind {
                    return index + 1 == number
                }
                return false
            }
        }

        return nil
    }

    private func isEntryDoor(_ candidate: DestinationCandidate) -> Bool {
        if case .door(_, let isEntry) = candidate.kind {
            return isEntry
        }
        return false
    }

    private static func buildCandidates(
        snapshot: CapturedRoomSnapshot,
        entryAnchor: EntryAnchorSnapshot,
        roomWorldTransform: simd_float4x4
    ) -> [DestinationCandidate] {
        let entryDoorIndex = resolvedEntryDoorIndex(snapshot: snapshot, entryAnchor: entryAnchor)
        var results: [DestinationCandidate] = []

        let objects = snapshot.objects.map { object -> (index: Int, category: String, label: String, roomPosition: SIMD3<Float>) in
            (
                index: object.index,
                category: object.category.lowercased(),
                label: RoomLabeling.displayName(for: object),
                roomPosition: RoomGeometry.translation(of: object.transformMatrix.simd)
            )
        }

        let bathroomObjects = objects.filter { ["toilet", "bathtub", "sink"].contains($0.category) }
        for cluster in clustered(items: bathroomObjects, threshold: 1.8) where !cluster.isEmpty {
            let center = averagePosition(of: cluster.map(\.roomPosition))
            results.append(
                DestinationCandidate(
                    id: UUID(),
                    name: "Bathroom",
                    aliases: ["bathroom", "restroom", "toilet", "sink", "bath", "washroom"],
                    kind: .zone,
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
                    aliases: ["seat", "seating", "chair", "table", "lounge", "dining"],
                    kind: .zone,
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
                    aliases: ["bed", "sleep", bed.label],
                    kind: .zone,
                    roomPosition: bed.roomPosition,
                    worldPosition: transform(bed.roomPosition, by: roomWorldTransform)
                )
            )
        }

        for door in snapshot.doors {
            let center = RoomGeometry.translation(of: door.transformMatrix.simd)
            let isEntry = door.index == entryDoorIndex
            let label = RoomLabeling.displayName(for: door)
            var aliases = [
                label,
                "door \(door.index + 1)",
                "door\(door.index + 1)"
            ]
            if isEntry {
                aliases += ["entry", "entry door", "front door", "exit", "entrance"]
            }

            results.append(
                DestinationCandidate(
                    id: UUID(),
                    name: label,
                    aliases: aliases,
                    kind: .door(index: door.index, isEntry: isEntry),
                    roomPosition: center,
                    worldPosition: transform(center, by: roomWorldTransform)
                )
            )
        }

        for object in objects {
            results.append(
                DestinationCandidate(
                    id: UUID(),
                    name: object.label,
                    aliases: [object.label, object.category, "\(object.category) \(object.index + 1)"],
                    kind: .object(index: object.index),
                    roomPosition: object.roomPosition,
                    worldPosition: transform(object.roomPosition, by: roomWorldTransform)
                )
            )
        }

        if snapshot.doors.isEmpty {
            let exitPosition = entryAnchor.positionMeters.simd
            results.append(
                DestinationCandidate(
                    id: UUID(),
                    name: "Exit",
                    aliases: ["exit", "door", "entrance", "entry", "front door", "out", "leave"],
                    kind: .zone,
                    roomPosition: exitPosition,
                    worldPosition: transform(exitPosition, by: roomWorldTransform)
                )
            )
        }

        return results
    }

    private static func resolvedEntryDoorIndex(
        snapshot: CapturedRoomSnapshot,
        entryAnchor: EntryAnchorSnapshot
    ) -> Int? {
        if let doorIndex = entryAnchor.doorIndex, snapshot.doors.contains(where: { $0.index == doorIndex }) {
            return doorIndex
        }

        guard !snapshot.doors.isEmpty else { return nil }
        let anchorPosition = entryAnchor.positionMeters.simd
        return snapshot.doors.min {
            let left = RoomGeometry.translation(of: $0.transformMatrix.simd)
            let right = RoomGeometry.translation(of: $1.transformMatrix.simd)
            return simd_distance(left, anchorPosition) < simd_distance(right, anchorPosition)
        }?.index
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
        items: [(index: Int, category: String, label: String, roomPosition: SIMD3<Float>)],
        threshold: Float
    ) -> [[(index: Int, category: String, label: String, roomPosition: SIMD3<Float>)]] {
        guard !items.isEmpty else { return [] }

        var remaining = Set(items.indices)
        var clusters: [[(index: Int, category: String, label: String, roomPosition: SIMD3<Float>)]] = []

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

    private static func sortRank(for candidate: DestinationCandidate) -> Int {
        switch candidate.kind {
        case .zone:
            switch candidate.name {
            case "Bathroom": return 0
            case "Seating Area": return 1
            case "Bed": return 2
            case "Exit": return 3
            default: return 4
            }
        case .door(let index, _):
            return 10 + index
        case .object(let index):
            return 100 + index
        }
    }

    private static func normalizedSearchKey(_ value: String) -> String {
        value
            .lowercased()
            .unicodeScalars
            .filter { CharacterSet.alphanumerics.contains($0) }
            .map(String.init)
            .joined()
    }

    private static func isExitQuery(_ normalizedQuery: String) -> Bool {
        ["exit", "door", "entrance", "entry", "frontdoor", "out", "leave"].contains {
            normalizedQuery.contains($0)
        }
    }
}
