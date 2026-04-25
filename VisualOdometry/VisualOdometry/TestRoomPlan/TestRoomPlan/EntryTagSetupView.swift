import SwiftUI
import RoomPlan
import UniformTypeIdentifiers

private enum RoomModelSetupState: Equatable {
    case idle
    case saving
    case complete(SavedRoomModelSummary, RoomModelEnvelope)
    case failure(String)
}

struct EntryTagSetupView: View {
    let capturedRoom: CapturedRoom

    @State private var selectedDoorIndex: Int?
    @State private var setupState: RoomModelSetupState = .idle
    @State private var showShareSheet = false
    @State private var showVisualMeshImporter = false
    @State private var roomIDPendingVisualMeshImport: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                headerCard

                FloorPlanView(capturedRoom: capturedRoom, selectedDoorIndex: selectedDoorIndex)
                    .frame(height: 360)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .overlay(
                        RoundedRectangle(cornerRadius: 16)
                            .stroke(DotsTheme.border, lineWidth: 1)
                    )
                    .accessibilityLabel("Floor plan preview")
                    .accessibilityHint("Highlights the currently selected entry door in yellow.")

                doorSelectionCard
                actionCard

                if case .complete(let summary, let envelope) = setupState {
                    savedModelCard(summary: summary, envelope: envelope)
                }
            }
            .padding()
        }
        .background(DotsTheme.background)
        .preferredColorScheme(.dark)
        .sheet(isPresented: $showShareSheet) {
            ActivitySheet(items: shareItems())
        }
        .fileImporter(
            isPresented: $showVisualMeshImporter,
            allowedContentTypes: [.usdz],
            allowsMultipleSelection: false
        ) { result in
            handleVisualMeshImport(result)
        }
        .onAppear {
            if selectedDoorIndex == nil, !capturedRoom.doors.isEmpty {
                selectedDoorIndex = 0
            }
        }
    }

    private var headerCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Confirm the entry door")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            Text("Select the doorway that represents the true room entry. This saved door transform becomes the anchor reference used to align navigation later.")
                .font(.subheadline)
                .foregroundStyle(DotsTheme.secondaryText)
        }
        .dotsPanel()
    }

    private var doorSelectionCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Detected Doors")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            if capturedRoom.doors.isEmpty {
                Label("No doors were detected in this scan. Start the scan at the entry and keep the doorway visible in the first pass, then rescan.", systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                    .foregroundStyle(.orange)
            } else {
                ForEach(Array(capturedRoom.doors.enumerated()), id: \.offset) { index, door in
                    Button {
                        selectedDoorIndex = index
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(index == selectedDoorIndex ? "Entry Door" : "Door \(index + 1)")
                                    .font(.headline)
                                    .foregroundStyle(DotsTheme.primaryText)

                                let position = RoomGeometry.translation(of: door.transform)
                                Text(String(format: "x %.2f m • z %.2f m", position.x, position.z))
                                    .font(.caption.monospaced())
                                    .foregroundStyle(DotsTheme.secondaryText)
                            }

                            Spacer()

                            Image(systemName: index == selectedDoorIndex ? "checkmark.circle.fill" : "circle")
                                .font(.system(size: 24))
                                .foregroundStyle(index == selectedDoorIndex ? .yellow : DotsTheme.secondaryText)
                        }
                        .padding(14)
                        .background(
                            RoundedRectangle(cornerRadius: 14)
                                .fill(index == selectedDoorIndex ? Color.yellow.opacity(0.12) : DotsTheme.panelStrong)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 14)
                                .stroke(index == selectedDoorIndex ? Color.yellow.opacity(0.8) : DotsTheme.border, lineWidth: 1)
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Door \(index + 1)")
                    .accessibilityHint("Marks this detected doorway as the entry anchor for future navigation.")
                }
            }
        }
        .dotsPanel()
    }

    private var actionCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Save Room Model")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            switch setupState {
            case .idle:
                Button {
                    Task { await saveRoomModelLocally() }
                } label: {
                    HStack {
                        Image(systemName: "internaldrive")
                        Text("Save Room Model on This Device")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(DotsPrimaryButtonStyle())
                .disabled(selectedDoorIndex == nil || capturedRoom.doors.isEmpty)

                if selectedDoorIndex == nil || capturedRoom.doors.isEmpty {
                    Text("Select a detected door before saving the room model.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }

            case .saving:
                HStack(spacing: 12) {
                    ProgressView()
                        .tint(.white)
                    Text("Saving the 3D room snapshot locally…")
                        .foregroundStyle(DotsTheme.secondaryText)
                }

            case .complete:
                Label("Room model saved locally. You can load it from Start Navigation.", systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
                    .font(.subheadline)

            case .failure(let message):
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.red)

                Button("Retry Save") {
                    Task { await saveRoomModelLocally() }
                }
                .buttonStyle(DotsSecondaryButtonStyle())
            }
        }
        .dotsPanel()
    }

    private func savedModelCard(summary: SavedRoomModelSummary, envelope: RoomModelEnvelope) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Saved Model")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            Text("Room ID: \(summary.roomID)")
                .font(.caption.monospaced())
                .foregroundStyle(DotsTheme.secondaryText)
                .textSelection(.enabled)

            Text("Saved: \(summary.savedAt.formatted(date: .abbreviated, time: .shortened))")
                .font(.subheadline)
                .foregroundStyle(DotsTheme.secondaryText)

            Text("\(summary.doorCount) doors • \(summary.objectCount) objects")
                .font(.subheadline)
                .foregroundStyle(DotsTheme.secondaryText)

            VStack(alignment: .leading, spacing: 8) {
                Text("3D Preview")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(DotsTheme.primaryText)

                Text("Drag to orbit the saved room snapshot. The entry door is highlighted in gold.")
                    .font(.caption)
                    .foregroundStyle(DotsTheme.secondaryText)

                RoomModelPreview3DView(envelope: envelope, visualMeshURL: summary.visualMeshURL)
                    .frame(height: 280)
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                    .overlay(
                        RoundedRectangle(cornerRadius: 14)
                            .stroke(DotsTheme.border, lineWidth: 1)
                    )
            }

            Button {
                showShareSheet = true
            } label: {
                HStack {
                    Image(systemName: "square.and.arrow.up")
                    Text("Share Saved JSON")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(DotsPrimaryButtonStyle())

            Button {
                roomIDPendingVisualMeshImport = summary.roomID
                showVisualMeshImporter = true
            } label: {
                HStack {
                    Image(systemName: summary.hasVisualMesh ? "square.and.arrow.down.badge.checkmark" : "square.and.arrow.down")
                    Text(summary.hasVisualMesh ? "Replace Imported USDZ" : "Import Polycam USDZ")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(DotsSecondaryButtonStyle())

            if let visualMeshFilename = summary.visualMeshFilename {
                Text("Imported mesh: \(visualMeshFilename)")
                    .font(.caption)
                    .foregroundStyle(.green)
                    .textSelection(.enabled)
            } else {
                Text("No visual mesh attached yet. Import a USDZ file to preview the room with photoreal geometry.")
                    .font(.caption)
                    .foregroundStyle(DotsTheme.secondaryText)
            }
        }
        .dotsPanel()
    }

    private func shareItems() -> [Any] {
        guard case .complete(let summary, let envelope) = setupState else { return [] }
        var items: [Any] = []
        items.append(summary.fileURL)
        if let exportURL = RoomModelExporter.saveJSON(envelope) {
            items.append(exportURL)
        }
        return items
    }

    @MainActor
    private func saveRoomModelLocally() async {
        guard let selectedDoorIndex else { return }
        setupState = .saving

        do {
            let localRoomID = UUID().uuidString.lowercased()
            let envelope = RoomModelExporter.makeEnvelope(
                room: capturedRoom,
                roomID: localRoomID,
                entryDoorIndex: selectedDoorIndex
            )
            let summary = try LocalRoomModelStore.shared.save(envelope: envelope)

            // Save the 2D floor plan image and compass metadata
            if let floorPlanImage = FloorPlanExporter.renderFloorPlanImage(
                capturedRoom: capturedRoom,
                selectedDoorIndex: selectedDoorIndex
            ) {
                let metadata = FloorPlanExporter.buildMetadata(
                    capturedRoom: capturedRoom,
                    roomID: localRoomID,
                    entryDoorIndex: selectedDoorIndex
                )
                try? FloorPlanExporter.saveFloorPlan(
                    image: floorPlanImage,
                    metadata: metadata,
                    roomID: localRoomID
                )
            }

            setupState = .complete(summary, envelope)
        } catch {
            setupState = .failure(error.localizedDescription)
        }
    }

    private func handleVisualMeshImport(_ result: Result<[URL], Error>) {
        guard
            let roomID = roomIDPendingVisualMeshImport,
            case .complete(_, let envelope) = setupState
        else {
            roomIDPendingVisualMeshImport = nil
            return
        }

        roomIDPendingVisualMeshImport = nil

        switch result {
        case .success(let urls):
            guard let sourceURL = urls.first else { return }
            do {
                _ = try LocalRoomModelStore.shared.importVisualMesh(from: sourceURL, for: roomID)
                if let refreshedSummary = try LocalRoomModelStore.shared.fetchSavedModels().first(where: { $0.roomID == roomID }) {
                    setupState = .complete(refreshedSummary, envelope)
                }
            } catch {
                setupState = .failure(error.localizedDescription)
            }
        case .failure(let error):
            setupState = .failure(error.localizedDescription)
        }
    }
}
