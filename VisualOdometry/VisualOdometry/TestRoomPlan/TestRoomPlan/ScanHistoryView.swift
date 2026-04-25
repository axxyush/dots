import SwiftUI

struct ScanHistoryView: View {
    @State private var rooms: [RoomSummary] = []
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        Group {
            if isLoading {
                ProgressView("Loading scans…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error = errorMessage {
                errorView(message: error)
            } else if rooms.isEmpty {
                emptyView
            } else {
                List(rooms) { room in
                    RoomHistoryRow(room: room)
                }
                .listStyle(.insetGrouped)
                .refreshable { await loadRooms() }
            }
        }
        .navigationTitle("My Scans")
        .task { await loadRooms() }
    }

    // MARK: - Sub-views

    private var emptyView: some View {
        VStack(spacing: 16) {
            Image(systemName: "tray")
                .font(.system(size: 48))
                .foregroundStyle(.secondary)
            Text("No scans yet")
                .font(.headline)
            Text("Complete a room scan and upload it to see it here.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorView(message: String) -> some View {
        VStack(spacing: 16) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 48))
                .foregroundStyle(.secondary)
            Text("Could not load scans")
                .font(.headline)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            Button("Retry") { Task { await loadRooms() } }
                .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Data

    private func loadRooms() async {
        isLoading = true
        errorMessage = nil
        do {
            rooms = try await BackendClient.shared.fetchRooms()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}

// MARK: - Room Row

struct RoomHistoryRow: View {
    let room: RoomSummary

    var body: some View {
        Button(action: openInDashboard) {
            VStack(alignment: .leading, spacing: 4) {
                Text(room.metadata.roomName)
                    .font(.headline)
                    .foregroundStyle(.primary)

                Text(room.metadata.buildingName)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                HStack {
                    Text(formattedDate(room.metadata.scannedAt))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    StatusBadge(status: room.status)
                }
                .padding(.top, 2)
            }
            .padding(.vertical, 2)
        }
        .foregroundStyle(.primary)
    }

    private func openInDashboard() {
        let urlString = "\(BackendClient.dashboardURL)/rooms/\(room.id)"
        guard let url = URL(string: urlString) else { return }
        UIApplication.shared.open(url)
    }

    private func formattedDate(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        let displayFormatter = DateFormatter()
        displayFormatter.dateStyle = .medium
        displayFormatter.timeStyle = .short

        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: iso) { return displayFormatter.string(from: date) }

        formatter.formatOptions = [.withInternetDateTime]
        if let date = formatter.date(from: iso) { return displayFormatter.string(from: date) }

        return iso
    }
}

// MARK: - Status Badge

struct StatusBadge: View {
    let status: String

    var body: some View {
        Text(status.capitalized)
            .font(.caption.bold())
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(color.opacity(0.15))
            .foregroundStyle(color)
            .clipShape(Capsule())
    }

    private var color: Color {
        switch status {
        case "received":            return .orange
        case "processed", "done":   return .green
        case "failed":              return .red
        default:                    return .secondary
        }
    }
}
