import Foundation

protocol RoomTagResolving {
    func resolveRoomID(from scannedValue: String) -> String?
}

struct QRRoomTagResolver: RoomTagResolving {
    func resolveRoomID(from scannedValue: String) -> String? {
        let trimmed = scannedValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if UUID(uuidString: trimmed) != nil {
            return trimmed.lowercased()
        }

        guard let url = URL(string: trimmed) else { return nil }
        if let roomIDItem = URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?
            .first(where: { $0.name == "room_id" })?
            .value,
           UUID(uuidString: roomIDItem) != nil {
            return roomIDItem.lowercased()
        }

        let candidate = url.lastPathComponent
        guard UUID(uuidString: candidate) != nil else { return nil }
        return candidate.lowercased()
    }
}
