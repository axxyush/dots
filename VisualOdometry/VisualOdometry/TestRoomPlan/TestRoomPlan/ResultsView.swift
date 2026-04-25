import SwiftUI
import RoomPlan

// MARK: - Upload State

enum UploadState: Equatable {
    case idle
    case uploading
    case processing(roomId: String)
    case complete(
        roomId: String,
        pdfUrl: String?,
        audioUrl: String?,
        recommendationsUrl: String?
    )
    case failure(message: String)
}

// MARK: - Results View

struct ResultsView: View {
    let capturedRoom: CapturedRoom
    let exportData: ScanExportData
    let onNewScan: () -> Void

    @State private var selectedTab = 2
    @State private var showingShare = false

    // Photo capture from scan phase
    @ObservedObject var scanManager: ScanManager

    // Upload metadata
    @State private var buildingName = ""
    @State private var roomName = ""

    // Upload flow
    @State private var uploadState: UploadState = .idle
    @State private var pipelineStep: String = "Uploading scan…"
    @State private var pollingTask: Task<Void, Never>?

    init(capturedRoom: CapturedRoom, scanDuration: TimeInterval, scanManager: ScanManager, onNewScan: @escaping () -> Void) {
        self.capturedRoom = capturedRoom
        self.exportData = RoomExporter.export(room: capturedRoom, scanDuration: scanDuration)
        self.scanManager = scanManager
        self.onNewScan = onNewScan
    }

    // MARK: - Body

    var body: some View {
        NavigationStack {
            ZStack {
                VStack(spacing: 0) {
                    Picker("View", selection: $selectedTab) {
                        Text("Floor Plan").tag(0)
                        Text("Report & Upload").tag(1)
                        Text("Entry & Save").tag(2)
                    }
                    .pickerStyle(.segmented)
                    .padding(.horizontal)
                    .padding(.top, 8)

                    if selectedTab == 0 {
                        FloorPlanView(capturedRoom: capturedRoom)
                    } else if selectedTab == 1 {
                        reportAndUploadList
                    } else {
                        EntryTagSetupView(capturedRoom: capturedRoom)
                    }
                }

                if case .uploading = uploadState {
                    UploadingOverlay()
                }
                if case .processing = uploadState {
                    ProcessingOverlay(step: pipelineStep)
                }
            }
            .navigationTitle("Scan Results")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("New Scan", action: onNewScan)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showingShare = true } label: {
                        Image(systemName: "square.and.arrow.up")
                    }
                }
            }
            .sheet(isPresented: $showingShare) { shareSheet }
        }
    }

    // MARK: - Report + Upload List

    private var reportAndUploadList: some View {
        List {
            uploadMetadataSection
            photoSection
            uploadActionSection

            // ── Existing report sections ──────────────────────────

            Section("Room Dimensions") {
                Text(exportData.accuracyReport.detectedRoomSize)
                    .font(.body.monospaced())
                Text(String(format: "Scan duration: %.1f s", exportData.accuracyReport.scanDurationSeconds))
            }

            Section("Detection Counts") {
                LabeledContent("Walls", value: "\(exportData.accuracyReport.wallCount)")
                LabeledContent("Doors", value: "\(exportData.accuracyReport.doorCount)")
                LabeledContent("Windows", value: "\(exportData.accuracyReport.windowCount)")
                LabeledContent("Objects", value: "\(exportData.accuracyReport.objectCount)")
            }

            if !exportData.accuracyReport.objectInventory.isEmpty {
                Section("Object Inventory") {
                    ForEach(Array(exportData.accuracyReport.objectInventory.enumerated()), id: \.offset) { _, item in
                        Text(item).font(.caption.monospaced())
                    }
                }
            }

            if !exportData.accuracyReport.distanceReport.isEmpty {
                Section("Distance Matrix (Floor Plane)") {
                    ForEach(Array(exportData.accuracyReport.distanceReport.enumerated()), id: \.offset) { _, item in
                        Text(item).font(.caption.monospaced())
                    }
                }
            }

            Section("Wall Details") {
                ForEach(exportData.walls, id: \.index) { wall in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Wall \(wall.index)").font(.subheadline.bold())
                        Text(String(format: "Position: (%.2f, %.2f, %.2f)", wall.positionX, wall.positionY, wall.positionZ))
                        Text(String(format: "Size: %.2f x %.2f m", wall.widthMeters, wall.heightMeters))
                    }
                    .font(.caption.monospaced())
                }
            }

            if !exportData.doors.isEmpty {
                Section("Door Details") {
                    ForEach(exportData.doors, id: \.index) { door in
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Door \(door.index)").font(.subheadline.bold())
                            Text(String(format: "Position: (%.2f, %.2f, %.2f)", door.positionX, door.positionY, door.positionZ))
                            Text(String(format: "Size: %.2f x %.2f m", door.widthMeters, door.heightMeters))
                            if let wallIdx = door.parentWallIndex { Text("Parent: Wall \(wallIdx)") }
                        }
                        .font(.caption.monospaced())
                    }
                }
            }

            if !exportData.windows.isEmpty {
                Section("Window Details") {
                    ForEach(exportData.windows, id: \.index) { win in
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Window \(win.index)").font(.subheadline.bold())
                            Text(String(format: "Position: (%.2f, %.2f, %.2f)", win.positionX, win.positionY, win.positionZ))
                            Text(String(format: "Size: %.2f x %.2f m", win.widthMeters, win.heightMeters))
                            if let wallIdx = win.parentWallIndex { Text("Parent: Wall \(wallIdx)") }
                        }
                        .font(.caption.monospaced())
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    // MARK: - Upload Metadata Section

    private var uploadMetadataSection: some View {
        Section {
            HStack {
                Text("Building")
                    .foregroundStyle(.secondary)
                    .frame(width: 72, alignment: .leading)
                TextField("Required", text: $buildingName)
                    .autocorrectionDisabled()
            }
            HStack {
                Text("Room")
                    .foregroundStyle(.secondary)
                    .frame(width: 72, alignment: .leading)
                TextField("Required", text: $roomName)
                    .autocorrectionDisabled()
            }
        } header: {
            Text("Room Information")
        } footer: {
            Text("These labels will appear in the Dots dashboard.")
                .font(.caption)
        }
    }

    // MARK: - Photo Section

    private var photoSection: some View {
        Section {
            if !scanManager.capturedPhotos.isEmpty {
                photoThumbnailRow
            } else {
                Text("No photos were taken during the scan. Please start a new scan to capture photos.")
                    .font(.caption)
                    .foregroundColor(.red)
            }

            if scanManager.capturedPhotos.count < 3 {
                Label(
                    scanManager.capturedPhotos.isEmpty
                        ? "Minimum 3 photos required for upload"
                        : "\(3 - scanManager.capturedPhotos.count) more photo(s) needed",
                    systemImage: "info.circle"
                )
                .font(.caption)
                .foregroundStyle(.orange)
            }
        } header: {
            Text("Room Photos (\(scanManager.capturedPhotos.count)/5)")
        } footer: {
            Text("Wide shots showing multiple objects. Used by AI to identify objects.")
                .font(.caption)
        }
    }

    private var photoThumbnailRow: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(scanManager.capturedPhotos.enumerated()), id: \.offset) { index, photo in
                    ZStack(alignment: .topTrailing) {
                        Image(uiImage: photo.image)
                            .resizable()
                            .scaledToFill()
                            .frame(width: 80, height: 80)
                            .clipped()
                            .cornerRadius(8)

                        Button {
                            scanManager.capturedPhotos.remove(at: index)
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .symbolRenderingMode(.palette)
                                .foregroundStyle(.white, .black.opacity(0.6))
                                .font(.system(size: 20))
                        }
                        .padding(3)
                    }
                }
            }
            .padding(.vertical, 4)
        }
        .frame(height: 96)
        .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
    }

    // MARK: - Upload Action Section

    private var uploadActionSection: some View {
        Section {
            switch uploadState {
            case .idle:
                Button {
                    Task { await performUpload() }
                } label: {
                    HStack {
                        Spacer()
                        Label("Upload Scan to Dots", systemImage: "arrow.up.to.line.circle.fill")
                            .font(.headline)
                        Spacer()
                    }
                }
                .disabled(!canUpload)

                if !canUpload {
                    requirementsLabel
                }

            case .uploading:
                HStack(spacing: 12) {
                    ProgressView()
                    Text("Uploading…")
                        .foregroundStyle(.secondary)
                }

            case .processing(let roomId):
                processingCard(roomId: roomId)

            case .complete(let roomId, let pdfUrl, let audioUrl, _):
                completeCard(roomId: roomId, pdfUrl: pdfUrl, audioUrl: audioUrl)

            case .failure(let message):
                failureCard(message: message)
            }
        } header: {
            Text("Upload")
        }
    }

    // MARK: - Upload Cards

    private func processingCard(roomId: String) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Generating Braille Map & Audio", systemImage: "gearshape.2.fill")
                .foregroundStyle(.blue)
                .font(.headline)

            Text("Room ID: \(roomId)")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)

            HStack(spacing: 12) {
                ProgressView()
                    .scaleEffect(0.8)
                Text(pipelineStep)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)

            Text("This usually takes 1–3 minutes. The AI agents are analyzing your photos, generating the Braille PDF, and creating an audio walkthrough.")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 4)
    }

    private func completeCard(roomId: String, pdfUrl: String?, audioUrl: String?) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Dots Ready!", systemImage: "checkmark.seal.fill")
                .foregroundStyle(.green)
                .font(.headline)

            Text("Room ID: \(roomId)")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)

            if let pdfUrl = pdfUrl,
               let url = URL(string: BackendClient.rewriteFileUrl(pdfUrl)) {
                Button {
                    UIApplication.shared.open(url)
                } label: {
                    HStack {
                        Image(systemName: "doc.text.fill")
                            .foregroundColor(.white)
                        Text("View Braille PDF")
                            .foregroundColor(.white)
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color.blue)
                    .cornerRadius(10)
                }
            }

            if let audioUrl = audioUrl,
               let url = URL(string: BackendClient.rewriteFileUrl(audioUrl)) {
                Button {
                    UIApplication.shared.open(url)
                } label: {
                    HStack {
                        Image(systemName: "headphones.circle.fill")
                            .foregroundColor(.white)
                        Text("Listen to Audio Walkthrough")
                            .foregroundColor(.white)
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color.purple)
                    .cornerRadius(10)
                }
            }

            if pdfUrl == nil && audioUrl == nil {
                Text("Processing completed but no files were generated. Check the backend logs.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            Button("Start New Scan", action: onNewScan)
                .buttonStyle(.bordered)
                .padding(.top, 4)
        }
        .padding(.vertical, 4)
    }

    private func failureCard(message: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Upload Failed", systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.red)
                .font(.headline)

            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)

            HStack(spacing: 12) {
                Button("Retry") {
                    Task { await performUpload() }
                }
                .buttonStyle(.borderedProminent)

                Button("Cancel") {
                    uploadState = .idle
                }
                .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }

    // MARK: - Requirements Label

    private var requirementsLabel: some View {
        VStack(alignment: .leading, spacing: 4) {
            if buildingName.trimmingCharacters(in: .whitespaces).isEmpty {
                Label("Building name required", systemImage: "exclamationmark.circle")
            }
            if roomName.trimmingCharacters(in: .whitespaces).isEmpty {
                Label("Room name required", systemImage: "exclamationmark.circle")
            }
            if scanManager.capturedPhotos.count < 3 {
                Label(
                    "At least 3 photos required (\(scanManager.capturedPhotos.count)/3)",
                    systemImage: "exclamationmark.circle"
                )
            }
        }
        .font(.caption)
        .foregroundStyle(.orange)
    }

    // MARK: - Computed

    private var canUpload: Bool {
        guard case .idle = uploadState else { return false }
        return scanManager.capturedPhotos.count >= 3
            && !buildingName.trimmingCharacters(in: .whitespaces).isEmpty
            && !roomName.trimmingCharacters(in: .whitespaces).isEmpty
    }

    // MARK: - Upload

    @MainActor
    private func performUpload() async {
        uploadState = .uploading
        do {
            let response = try await BackendClient.shared.uploadScan(
                scanData: exportData,
                photos: scanManager.capturedPhotos,
                roomName: roomName.trimmingCharacters(in: .whitespaces),
                buildingName: buildingName.trimmingCharacters(in: .whitespaces)
            )
            let roomId = response.roomId
            pipelineStep = "Pipeline triggered, processing scan…"
            uploadState = .processing(roomId: roomId)

            // Start polling for completion
            pollingTask = Task {
                await pollUntilComplete(roomId: roomId)
            }
        } catch {
            uploadState = .failure(message: error.localizedDescription)
        }
    }

    @MainActor
    private func pollUntilComplete(roomId: String) async {
        let maxAttempts = 90  // ~4.5 minutes at 3s intervals
        for attempt in 1...maxAttempts {
            if Task.isCancelled { return }

            do {
                let status = try await BackendClient.shared.pollRoomStatus(roomId: roomId)

                // Update step text based on status
                switch status.status {
                case "received", "pipeline_triggered":
                    pipelineStep = "Analyzing room layout…"
                case "spatial_processed":
                    pipelineStep = "AI identifying objects from photos…"
                case "enriched", "enriched_no_objects":
                    pipelineStep = "Generating Braille PDF & audio…"
                default:
                    if status.status.starts(with: "error") {
                        uploadState = .failure(message: "Pipeline error: \(status.status)")
                        return
                    }
                    pipelineStep = "Processing (\(status.status))…"
                }

                if status.isComplete {
                    uploadState = .complete(
                        roomId: roomId,
                        pdfUrl: status.pdfUrl,
                        audioUrl: status.audioUrl,
                        recommendationsUrl: status.recommendationsPdfUrl
                    )
                    return
                }

                // Check partial completion
                if status.statusMapDone && !status.statusNarrationDone {
                    pipelineStep = "Braille PDF ready! Generating audio…"
                } else if !status.statusMapDone && status.statusNarrationDone {
                    pipelineStep = "Audio ready! Generating Braille PDF…"
                }
            } catch {
                // Ignore transient errors, keep polling
                if attempt > 5 {
                    pipelineStep = "Still working (retrying)…"
                }
            }

            try? await Task.sleep(nanoseconds: 3_000_000_000) // 3 seconds
        }

        // Timed out
        uploadState = .failure(message: "Pipeline timed out after ~4 minutes. Check the backend.")
    }

    // MARK: - Share Sheet

    @ViewBuilder
    private var shareSheet: some View {
        ActivitySheet(items: exportItems())
    }

    private func exportItems() -> [Any] {
        var items: [Any] = []
        if let url = RoomExporter.saveJSON(exportData) { items.append(url) }
        let planView = FloorPlanView(capturedRoom: capturedRoom)
            .frame(width: 800, height: 800)
            .background(Color.white)
        let renderer = ImageRenderer(content: planView)
        renderer.scale = 3
        if let image = renderer.uiImage { items.append(image) }
        return items
    }
}

// MARK: - UIActivityViewController Wrapper

struct ActivitySheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}
