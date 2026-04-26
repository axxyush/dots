import SwiftUI
import RoomPlan
import UniformTypeIdentifiers

private enum RoomModelSetupState: Equatable {
    case idle
    case saving
    case complete(SavedRoomModelSummary, RoomModelEnvelope)
    case failure(String)
}

private enum RenameTarget: Identifiable, Equatable {
    case door(Int)
    case object(Int)

    var id: String {
        switch self {
        case .door(let index):
            return "door-\(index)"
        case .object(let index):
            return "object-\(index)"
        }
    }
}

struct EntryTagSetupView: View {
    let capturedRoom: CapturedRoom

    @State private var selectedAnchor: SelectedAnchor?
    @State private var doorLabelOverrides: [Int: String] = [:]
    @State private var objectLabelOverrides: [Int: String] = [:]
    @State private var setupState: RoomModelSetupState = .idle
    @State private var showShareSheet = false
    @State private var showVisualMeshImporter = false
    @State private var roomIDPendingVisualMeshImport: String?
    @State private var renameTarget: RenameTarget?
    @State private var renameDraft = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                headerCard

                FloorPlanView(
                    capturedRoom: capturedRoom,
                    selectedAnchor: selectedAnchor,
                    doorLabelOverrides: doorLabelOverrides,
                    objectLabelOverrides: objectLabelOverrides,
                    onSelectAnchor: { selectedAnchor = $0 }
                )
                    .frame(height: 360)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .overlay(
                        RoundedRectangle(cornerRadius: 16)
                            .stroke(DotsTheme.border, lineWidth: 1)
                    )
                    .accessibilityLabel("Floor plan preview")
                    .accessibilityHint("Tap a detected door or object to select it and relabel it before saving.")

                selectionDetailCard
                doorSelectionCard
                objectSelectionCard
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
        .sheet(item: $renameTarget) { target in
            renameSheet(for: target)
        }
        .fileImporter(
            isPresented: $showVisualMeshImporter,
            allowedContentTypes: [.usdz],
            allowsMultipleSelection: false
        ) { result in
            handleVisualMeshImport(result)
        }
        .onAppear {
            if selectedAnchor == nil {
                if !capturedRoom.doors.isEmpty {
                    selectedAnchor = .door(0)
                } else if !capturedRoom.objects.isEmpty {
                    selectedAnchor = .object(0)
                }
            }
        }
    }

    private var headerCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Confirm the starting point")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            Text("Tap the floor plan or use the lists below to choose the true room entry. You can relabel detected doors and objects before saving so navigation uses clearer names.")
                .font(.subheadline)
                .foregroundStyle(DotsTheme.secondaryText)
        }
        .dotsPanel()
    }

    private var selectionDetailCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Selected Element")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            if let selectedAnchor {
                Text(selectedAnchorDisplayName)
                    .font(.headline)
                    .foregroundStyle(DotsTheme.primaryText)

                Text("This highlighted element will be used as the saved starting point unless you choose something else.")
                    .font(.subheadline)
                    .foregroundStyle(DotsTheme.secondaryText)

                Button {
                    presentRenameSheet(for: selectedAnchor)
                } label: {
                    HStack {
                        Image(systemName: "pencil")
                        Text("Rename Selected Element")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(DotsSecondaryButtonStyle())
            } else {
                Text("Tap a door or object on the floor plan to select it.")
                    .font(.subheadline)
                    .foregroundStyle(DotsTheme.secondaryText)
            }
        }
        .dotsPanel()
    }

    private var doorSelectionCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Detected Doors")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            if capturedRoom.doors.isEmpty {
                Label("No doors were detected in this scan.", systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                    .foregroundStyle(.orange)
            } else {
                ForEach(Array(capturedRoom.doors.enumerated()), id: \.offset) { index, door in
                    let isSelected = selectedAnchor == .door(index)
                    Button {
                        selectedAnchor = .door(index)
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(isSelected ? "Starting Point (\(doorLabel(for: index)))" : doorLabel(for: index))
                                    .font(.headline)
                                    .foregroundStyle(DotsTheme.primaryText)

                                let position = RoomGeometry.translation(of: door.transform)
                                Text(String(format: "x %.2f m • z %.2f m", position.x, position.z))
                                    .font(.caption.monospaced())
                                    .foregroundStyle(DotsTheme.secondaryText)
                            }

                            Spacer()

                            Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                                .font(.system(size: 24))
                                .foregroundStyle(isSelected ? .yellow : DotsTheme.secondaryText)
                        }
                        .padding(14)
                        .background(
                            RoundedRectangle(cornerRadius: 14)
                                .fill(isSelected ? Color.yellow.opacity(0.12) : DotsTheme.panelStrong)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 14)
                                .stroke(isSelected ? Color.yellow.opacity(0.8) : DotsTheme.border, lineWidth: 1)
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Door \(index + 1)")
                    .accessibilityHint("Marks this detected doorway as the starting point for future navigation.")
                }
            }
        }
        .dotsPanel()
    }

    private var objectSelectionCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Detected Objects")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            if capturedRoom.objects.isEmpty {
                Label("No objects were detected in this scan.", systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                    .foregroundStyle(.orange)
            } else {
                ForEach(Array(capturedRoom.objects.enumerated()), id: \.offset) { index, object in
                    let isSelected = selectedAnchor == .object(index)
                    Button {
                        selectedAnchor = .object(index)
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(isSelected ? "Starting Point (\(objectLabel(for: index, object: object)))" : objectLabel(for: index, object: object))
                                    .font(.headline)
                                    .foregroundStyle(DotsTheme.primaryText)

                                let position = RoomGeometry.translation(of: object.transform)
                                Text(String(format: "x %.2f m • z %.2f m", position.x, position.z))
                                    .font(.caption.monospaced())
                                    .foregroundStyle(DotsTheme.secondaryText)
                            }

                            Spacer()

                            Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                                .font(.system(size: 24))
                                .foregroundStyle(isSelected ? .yellow : DotsTheme.secondaryText)
                        }
                        .padding(14)
                        .background(
                            RoundedRectangle(cornerRadius: 14)
                                .fill(isSelected ? Color.yellow.opacity(0.12) : DotsTheme.panelStrong)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 14)
                                .stroke(isSelected ? Color.yellow.opacity(0.8) : DotsTheme.border, lineWidth: 1)
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(objectLabel(for: index, object: object))
                    .accessibilityHint("Marks this detected object as the starting point for future navigation.")
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
                .disabled(selectedAnchor == nil)

                if selectedAnchor == nil {
                    Text("Select a detected door or object before saving the room model.")
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

                Text("Drag to orbit the saved room snapshot. The starting point is highlighted in gold.")
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
        guard let selectedAnchor else { return }
        setupState = .saving

        do {
            let localRoomID = UUID().uuidString.lowercased()
            let envelope = RoomModelExporter.makeEnvelope(
                room: capturedRoom,
                roomID: localRoomID,
                selectedAnchor: selectedAnchor,
                doorLabelOverrides: doorLabelOverrides,
                objectLabelOverrides: objectLabelOverrides
            )
            let summary = try LocalRoomModelStore.shared.save(envelope: envelope)

            // Save the 2D floor plan image and compass metadata
            if let floorPlanImage = FloorPlanExporter.renderFloorPlanImage(
                capturedRoom: capturedRoom,
                selectedAnchor: selectedAnchor,
                doorLabelOverrides: doorLabelOverrides,
                objectLabelOverrides: objectLabelOverrides
            ) {
                let metadata = FloorPlanExporter.buildMetadata(
                    capturedRoom: capturedRoom,
                    roomID: localRoomID,
                    selectedAnchor: selectedAnchor,
                    doorLabelOverrides: doorLabelOverrides,
                    objectLabelOverrides: objectLabelOverrides
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

    @ViewBuilder
    private func renameSheet(for target: RenameTarget) -> some View {
        NavigationStack {
            Form {
                Section("Label") {
                    TextField("Enter a label", text: $renameDraft)
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled()

                    Text("Leave this blank to fall back to the default generated label.")
                        .font(.caption)
                        .foregroundStyle(DotsTheme.secondaryText)
                }
            }
            .navigationTitle(renameTitle(for: target))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") {
                        renameTarget = nil
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Save") {
                        applyRename(for: target, value: renameDraft)
                        renameTarget = nil
                    }
                }
            }
        }
        .preferredColorScheme(.dark)
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

    private var selectedAnchorDisplayName: String {
        guard let selectedAnchor else { return "Nothing selected" }
        switch selectedAnchor {
        case .door(let index):
            return doorLabel(for: index)
        case .object(let index):
            guard capturedRoom.objects.indices.contains(index) else {
                return RoomLabeling.defaultObjectLabel(category: "Object", index: index)
            }
            return objectLabel(for: index, object: capturedRoom.objects[index])
        }
    }

    private func doorLabel(for index: Int) -> String {
        RoomLabeling.sanitizedOverride(doorLabelOverrides[index]) ?? RoomLabeling.defaultSurfaceLabel(category: "door", index: index)
    }

    private func objectLabel(for index: Int, object: CapturedRoom.Object) -> String {
        let category = RoomExporter.objectCategoryName(object.category)
        return RoomLabeling.sanitizedOverride(objectLabelOverrides[index]) ?? RoomLabeling.defaultObjectLabel(category: category, index: index)
    }

    private func presentRenameSheet(for selectedAnchor: SelectedAnchor) {
        switch selectedAnchor {
        case .door(let index):
            renameDraft = RoomLabeling.sanitizedOverride(doorLabelOverrides[index]) ?? ""
            renameTarget = .door(index)
        case .object(let index):
            renameDraft = RoomLabeling.sanitizedOverride(objectLabelOverrides[index]) ?? ""
            renameTarget = .object(index)
        }
    }

    private func renameTitle(for target: RenameTarget) -> String {
        switch target {
        case .door(let index):
            return "Rename \(doorLabel(for: index))"
        case .object(let index):
            guard capturedRoom.objects.indices.contains(index) else { return "Rename Object" }
            return "Rename \(objectLabel(for: index, object: capturedRoom.objects[index]))"
        }
    }

    private func applyRename(for target: RenameTarget, value: String) {
        let sanitized = RoomLabeling.sanitizedOverride(value)
        switch target {
        case .door(let index):
            doorLabelOverrides[index] = sanitized
        case .object(let index):
            objectLabelOverrides[index] = sanitized
        }
    }
}
