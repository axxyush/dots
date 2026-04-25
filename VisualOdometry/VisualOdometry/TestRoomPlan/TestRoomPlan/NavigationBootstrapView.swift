import SwiftUI
import UniformTypeIdentifiers

private enum NavigationBootstrapState: Equatable {
    case browsing
    case loading(roomID: String)
    case anchoring(RoomModelEnvelope, URL?)
    case failure(String)
}

private struct PreviewEnvelopeItem: Identifiable {
    let id = UUID()
    let envelope: RoomModelEnvelope
    let visualMeshURL: URL?
}

struct NavigationBootstrapView: View {
    let onDismiss: () -> Void

    @State private var state: NavigationBootstrapState = .browsing
    @State private var savedModels: [SavedRoomModelSummary] = []
    @State private var previewEnvelope: PreviewEnvelopeItem?
    @State private var roomIDPendingImport: String?
    @State private var isPresentingVisualMeshImporter = false
    @State private var roomIDPendingDeletion: String?
    @State private var showDeleteConfirmation = false

    var body: some View {
        NavigationStack {
            ZStack {
                switch state {
                case .browsing:
                    savedModelsScreen
                case .loading(let roomID):
                    loadingScreen(roomID: roomID)
                case .anchoring(let envelope, let visualMeshURL):
                    RoomAnchoringView(envelope: envelope, visualMeshURL: visualMeshURL) {
                        state = .browsing
                        Task { await refreshSavedModels() }
                    }
                case .failure(let message):
                    failureScreen(message: message)
                }
            }
            .navigationTitle("Start Navigation")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Back", action: onDismiss)
                }
            }
        }
        .preferredColorScheme(.dark)
        .background(DotsTheme.background)
        .task {
            await refreshSavedModels()
        }
        .sheet(item: $previewEnvelope) { item in
            RoomModelPreviewSheet(envelope: item.envelope, visualMeshURL: item.visualMeshURL)
        }
        .fileImporter(
            isPresented: $isPresentingVisualMeshImporter,
            allowedContentTypes: [.usdz],
            allowsMultipleSelection: false
        ) { result in
            handleVisualMeshImport(result)
        }
        .confirmationDialog(
            "Delete Room Model",
            isPresented: $showDeleteConfirmation,
            titleVisibility: .visible
        ) {
            Button("Delete", role: .destructive) {
                if let roomID = roomIDPendingDeletion {
                    deleteModel(roomID: roomID)
                }
            }
            Button("Cancel", role: .cancel) {
                roomIDPendingDeletion = nil
            }
        } message: {
            Text("This will permanently remove the room model, floor plan, and any imported visual mesh. This cannot be undone.")
        }
    }

    private var savedModelsScreen: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Load a saved room model.")
                        .font(.headline)
                        .foregroundStyle(DotsTheme.primaryText)
                    Text("For local testing, pick a room model that was saved during setup, stand at that room's entry, then align the model from your current camera pose.")
                        .font(.subheadline)
                        .foregroundStyle(DotsTheme.secondaryText)
                }
                .dotsPanel()

                if savedModels.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("No saved room models yet.")
                            .font(.headline)
                            .foregroundStyle(DotsTheme.primaryText)
                        Text("Finish a RoomPlan scan, confirm the entry door, and save the room model first.")
                            .font(.subheadline)
                            .foregroundStyle(DotsTheme.secondaryText)
                    }
                    .dotsPanel()
                } else {
                    ForEach(savedModels) { model in
                        HStack(spacing: 10) {
                            Button {
                                loadSavedModel(roomID: model.roomID)
                            } label: {
                                HStack(alignment: .top, spacing: 14) {
                                    Image(systemName: "cube.transparent")
                                        .font(.system(size: 24, weight: .semibold))
                                        .foregroundStyle(.yellow)
                                        .frame(width: 32, height: 32)

                                    VStack(alignment: .leading, spacing: 6) {
                                        Text(model.title)
                                            .font(.headline)
                                            .foregroundStyle(DotsTheme.primaryText)

                                        Text("Saved \(model.savedAt.formatted(date: .abbreviated, time: .shortened))")
                                            .font(.subheadline)
                                            .foregroundStyle(DotsTheme.secondaryText)

                                        Text("\(model.doorCount) doors • \(model.objectCount) objects")
                                            .font(.caption.monospaced())
                                            .foregroundStyle(DotsTheme.secondaryText)

                                        Text(model.hasVisualMesh ? "Visual mesh attached" : "No visual mesh")
                                            .font(.caption)
                                            .foregroundStyle(model.hasVisualMesh ? .green : DotsTheme.secondaryText)
                                    }

                                    Spacer()

                                    Image(systemName: "chevron.right")
                                        .foregroundStyle(DotsTheme.secondaryText)
                                }
                                .padding(16)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(
                                    RoundedRectangle(cornerRadius: 16)
                                        .fill(DotsTheme.panel)
                                )
                                .overlay(
                                    RoundedRectangle(cornerRadius: 16)
                                        .stroke(DotsTheme.border, lineWidth: 1)
                                )
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(model.title)
                            .accessibilityHint("Loads this saved room model for local navigation alignment.")

                            Button {
                                showPreview(roomID: model.roomID)
                            } label: {
                                Image(systemName: "eye.fill")
                                    .font(.system(size: 20, weight: .semibold))
                                    .foregroundStyle(.white)
                                    .frame(width: 60, height: 60)
                                    .background(
                                        RoundedRectangle(cornerRadius: 16)
                                            .fill(DotsTheme.panelStrong)
                                    )
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 16)
                                            .stroke(DotsTheme.border, lineWidth: 1)
                                    )
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Preview \(model.title)")
                            .accessibilityHint("Opens a 3D preview from the saved entry-door point of view.")

                            Button {
                                beginVisualMeshImport(for: model.roomID)
                            } label: {
                                Image(systemName: model.hasVisualMesh ? "square.and.arrow.down.badge.checkmark" : "square.and.arrow.down")
                                    .font(.system(size: 20, weight: .semibold))
                                    .foregroundStyle(.white)
                                    .frame(width: 60, height: 60)
                                    .background(
                                        RoundedRectangle(cornerRadius: 16)
                                            .fill(DotsTheme.panelStrong)
                                    )
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 16)
                                            .stroke(DotsTheme.border, lineWidth: 1)
                                    )
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(model.hasVisualMesh ? "Replace visual mesh for \(model.title)" : "Import visual mesh for \(model.title)")
                            .accessibilityHint("Imports a USDZ file, such as a Polycam export, and stores it with this saved room model.")

                            Button {
                                roomIDPendingDeletion = model.roomID
                                showDeleteConfirmation = true
                            } label: {
                                Image(systemName: "trash.fill")
                                    .font(.system(size: 18, weight: .semibold))
                                    .foregroundStyle(.red.opacity(0.9))
                                    .frame(width: 60, height: 60)
                                    .background(
                                        RoundedRectangle(cornerRadius: 16)
                                            .fill(Color.red.opacity(0.1))
                                    )
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 16)
                                            .stroke(Color.red.opacity(0.3), lineWidth: 1)
                                    )
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Delete \(model.title)")
                            .accessibilityHint("Permanently removes this room model and all associated data.")
                        }
                    }
                }
            }
            .padding()
        }
    }

    private func loadingScreen(roomID: String) -> some View {
        VStack(spacing: 16) {
            ProgressView()
                .tint(.white)
                .scaleEffect(1.3)

            Text("Loading room model")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            Text(roomID)
                .font(.caption.monospaced())
                .foregroundStyle(DotsTheme.secondaryText)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(DotsTheme.background)
    }

    private func failureScreen(message: String) -> some View {
        VStack(spacing: 18) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 42))
                .foregroundStyle(.orange)

            Text("Could not load the room model")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            Text(message)
                .font(.subheadline)
                .foregroundStyle(DotsTheme.secondaryText)
                .multilineTextAlignment(.center)
                .padding(.horizontal)

            Button("Try Again") {
                state = .browsing
                Task { await refreshSavedModels() }
            }
            .buttonStyle(DotsPrimaryButtonStyle())
            .frame(maxWidth: 320)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(DotsTheme.background)
    }

    private func loadSavedModel(roomID: String) {
        state = .loading(roomID: roomID)
        Task {
            await loadRoomModel(roomID: roomID)
        }
    }

    private func showPreview(roomID: String) {
        do {
            previewEnvelope = PreviewEnvelopeItem(
                envelope: try LocalRoomModelStore.shared.loadEnvelope(roomID: roomID),
                visualMeshURL: LocalRoomModelStore.shared.visualMeshURL(roomID: roomID)
            )
        } catch {
            state = .failure(error.localizedDescription)
        }
    }

    private func beginVisualMeshImport(for roomID: String) {
        roomIDPendingImport = roomID
        isPresentingVisualMeshImporter = true
    }

    @MainActor
    private func refreshSavedModels() async {
        do {
            savedModels = try LocalRoomModelStore.shared.fetchSavedModels()
        } catch {
            state = .failure(error.localizedDescription)
        }
    }

    @MainActor
    private func loadRoomModel(roomID: String) async {
        do {
            let envelope = try LocalRoomModelStore.shared.loadEnvelope(roomID: roomID)
            let visualMeshURL = LocalRoomModelStore.shared.visualMeshURL(roomID: roomID)
            state = .anchoring(envelope, visualMeshURL)
        } catch {
            state = .failure(error.localizedDescription)
        }
    }

    private func handleVisualMeshImport(_ result: Result<[URL], Error>) {
        guard let roomID = roomIDPendingImport else { return }
        roomIDPendingImport = nil

        switch result {
        case .success(let urls):
            guard let sourceURL = urls.first else { return }
            do {
                _ = try LocalRoomModelStore.shared.importVisualMesh(from: sourceURL, for: roomID)
                Task { await refreshSavedModels() }
            } catch {
                state = .failure(error.localizedDescription)
            }
        case .failure(let error):
            state = .failure(error.localizedDescription)
        }
    }

    private func deleteModel(roomID: String) {
        do {
            try LocalRoomModelStore.shared.delete(roomID: roomID)
            roomIDPendingDeletion = nil
            Task { await refreshSavedModels() }
        } catch {
            state = .failure(error.localizedDescription)
        }
    }
}

private struct RoomModelPreviewSheet: View {
    let envelope: RoomModelEnvelope
    let visualMeshURL: URL?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ZStack {
                DotsTheme.background.ignoresSafeArea()

                RoomModelPreview3DView(
                    envelope: envelope,
                    cameraPreset: RoomPreviewCameraPreset.entryDoor,
                    visualMeshURL: visualMeshURL
                )
                    .ignoresSafeArea()
            }
            .navigationTitle("Entry View")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}
