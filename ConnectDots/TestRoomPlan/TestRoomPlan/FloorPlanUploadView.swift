import SwiftUI
import PhotosUI
import WebKit
import UIKit

// MARK: - Floor Plan Upload View

struct FloorPlanUploadView: View {
    let onDismiss: () -> Void

    @State private var selectedImage: UIImage?
    @State private var showingPicker = false
    @State private var buildingName = ""
    @State private var floorName = ""

    @State private var uploadState: UploadState = .idle
    @State private var pipelineStep = "Uploading floor plan..."
    @State private var pollingTask: Task<Void, Never>?
    @State private var visualAgentTask: Task<Void, Never>?
    @State private var visualAgentIndex = 0
    @State private var visualProgress: Double = 0
    @State private var visualTick = 0

    @State private var showReportHub = false
    @State private var showTalkToAgent = false
    @FocusState private var focusedField: ActiveField?

    private let agentStages = [
        "Parsing floor plan",
        "Detecting walls & doors",
        "Mapping ADA paths",
        "Generating Braille tiles",
        "Compiling audio cues"
    ]

    private enum ActiveField: Hashable {
        case building
        case floor
    }

    var body: some View {
        NavigationStack {
            ZStack {
                DotsTheme.background.ignoresSafeArea()

                if let completed = completedResult {
                    completionView(roomId: completed.roomId)
                        .padding(.horizontal, 20)
                } else {
                    formContent
                }

                if case .uploading = uploadState {
                    uploadingOverlay
                }

                if case .processing = uploadState {
                    processingOverlay
                }
            }
            .navigationTitle("Upload Floor Plan")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Back", action: onDismiss)
                        .foregroundStyle(DotsTheme.primaryText)
                }
            }
            .toolbarColorScheme(.dark, for: .navigationBar)
            .sheet(isPresented: $showingPicker) {
                FloorPlanImagePicker(image: $selectedImage)
            }
            .navigationDestination(isPresented: $showReportHub) {
                if let completed = completedResult {
                    DotsReportHubView(
                        roomId: completed.roomId,
                        pdfUrl: completed.pdfUrl,
                        recommendationsUrl: completed.recommendationsUrl,
                        onTalkToAgent: { showTalkToAgent = true }
                    )
                } else {
                    Text("Report unavailable")
                        .foregroundStyle(DotsTheme.primaryText)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(DotsTheme.background)
                }
            }
            .fullScreenCover(isPresented: $showTalkToAgent) {
                if let roomId = completedRoomId {
                    TalkToAgentView(roomId: roomId) {
                        showTalkToAgent = false
                    }
                }
            }
            .onDisappear {
                pollingTask?.cancel()
                stopSimulatedAgentSequence()
            }
        }
        .preferredColorScheme(.dark)
    }

    // MARK: - Form Content

    private var formContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                DotsWordmark(textSize: 28, dotDiameter: 7, weight: .semibold)
                    .padding(.bottom, 4)

                Text("Upload a PNG floor plan, name the building and floor, then create your ADA-ready output.")
                    .font(.subheadline)
                    .foregroundStyle(DotsTheme.secondaryText)

                imageSelectionCard
                metadataCard
                createCard
                processingStatusCard
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 20)
        }
    }

    private var imageSelectionCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Floor Plan Image")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            if let image = selectedImage {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .frame(maxHeight: 250)
                    .clipShape(RoundedRectangle(cornerRadius: 14))

                HStack(spacing: 10) {
                    Button("Change") {
                        showingPicker = true
                    }
                    .buttonStyle(DotsSecondaryButtonStyle())

                    Button("Remove") {
                        selectedImage = nil
                    }
                    .buttonStyle(DotsSecondaryButtonStyle())
                }
            } else {
                Button {
                    showingPicker = true
                } label: {
                    VStack(spacing: 12) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 34, weight: .medium))
                            .foregroundStyle(DotsTheme.primaryText)

                        Text("Select Floor Plan")
                            .font(.headline)
                            .foregroundStyle(DotsTheme.primaryText)

                        Text("PNG, JPG, and JPEG are accepted")
                            .font(.caption)
                            .foregroundStyle(DotsTheme.secondaryText)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 26)
                }
                .buttonStyle(DotsSecondaryButtonStyle())
            }
        }
        .dotsPanel()
    }

    private var metadataCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Details")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            dotsField(title: "Building", placeholder: "e.g. Downtown Library", text: $buildingName, field: .building)
            dotsField(title: "Floor", placeholder: "e.g. Ground Floor", text: $floorName, field: .floor)

            Text("These labels are used in the generated report and tactile map.")
                .font(.caption)
                .foregroundStyle(DotsTheme.tertiaryText)
        }
        .dotsPanel()
    }

    private var createCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Create")
                .font(.headline)
                .foregroundStyle(DotsTheme.primaryText)

            switch uploadState {
            case .idle:
                Button {
                    focusedField = nil
                    hideKeyboard()
                    Task { await performUpload() }
                } label: {
                    HStack {
                        Image(systemName: "sparkles")
                        Text("Create")
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(DotsPrimaryButtonStyle())
                .disabled(!canUpload)

                if !canUpload {
                    requirementsLabel
                }

            case .uploading, .processing:
                HStack(spacing: 10) {
                    ProgressView()
                        .tint(.white)
                    Text("Working...")
                        .foregroundStyle(DotsTheme.secondaryText)
                }

            case .failure(let message):
                VStack(alignment: .leading, spacing: 10) {
                    Text("Could not create output")
                        .font(.headline)
                        .foregroundStyle(.red)

                    Text(message)
                        .font(.caption)
                        .foregroundStyle(DotsTheme.secondaryText)

                    HStack(spacing: 12) {
                        Button("Retry") {
                            Task { await performUpload() }
                        }
                        .buttonStyle(DotsPrimaryButtonStyle())

                        Button("Reset") {
                            uploadState = .idle
                        }
                        .buttonStyle(DotsSecondaryButtonStyle())
                    }
                }

            case .complete:
                EmptyView()
            }
        }
        .dotsPanel()
    }

    private var processingStatusCard: some View {
        Group {
            if case .processing(let roomId) = uploadState {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Processing")
                        .font(.headline)
                        .foregroundStyle(DotsTheme.primaryText)

                    Text("Room ID: \(roomId)")
                        .font(.caption.monospaced())
                        .foregroundStyle(DotsTheme.secondaryText)
                        .textSelection(.enabled)

                    Text(pipelineStep)
                        .font(.subheadline)
                        .foregroundStyle(DotsTheme.secondaryText)

                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(Array(agentStages.enumerated()), id: \.offset) { index, stage in
                            HStack(spacing: 8) {
                                agentStageIcon(for: index)
                                Text(stage)
                                    .font(.caption.weight(.medium))
                                    .foregroundStyle(agentStageTextColor(for: index))
                            }
                        }
                    }
                    .padding(.top, 4)
                }
                .dotsPanel()
            }
        }
    }

    private var requirementsLabel: some View {
        VStack(alignment: .leading, spacing: 4) {
            if selectedImage == nil {
                Label("Floor plan image required", systemImage: "exclamationmark.circle")
            }
            if buildingName.trimmingCharacters(in: .whitespaces).isEmpty {
                Label("Building name required", systemImage: "exclamationmark.circle")
            }
            if floorName.trimmingCharacters(in: .whitespaces).isEmpty {
                Label("Floor name required", systemImage: "exclamationmark.circle")
            }
        }
        .font(.caption)
        .foregroundStyle(Color.red.opacity(0.9))
    }

    private func completionView(roomId: String) -> some View {
        VStack(spacing: 22) {
            Spacer()

            DotsWordmark(textSize: 34, dotDiameter: 8, weight: .semibold)

            VStack(spacing: 8) {
                Text("Your output is ready")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(DotsTheme.primaryText)

                Text("Room ID: \(roomId)")
                    .font(.caption.monospaced())
                    .foregroundStyle(DotsTheme.secondaryText)
                    .textSelection(.enabled)
            }

            Button {
                showReportHub = true
            } label: {
                HStack {
                    Image(systemName: "doc.text.magnifyingglass")
                    Text("View Report")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(DotsPrimaryButtonStyle())

            Button("Back to Home") {
                onDismiss()
            }
            .frame(maxWidth: .infinity)
            .buttonStyle(DotsSecondaryButtonStyle())

            Spacer()
        }
    }

    private func dotsField(title: String, placeholder: String, text: Binding<String>, field: ActiveField) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(DotsTheme.secondaryText)

            TextField(placeholder, text: text)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.words)
                .focused($focusedField, equals: field)
                .padding(.horizontal, 12)
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 12)
                        .fill(DotsTheme.panelStrong)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .stroke(DotsTheme.border, lineWidth: 1)
                )
                .foregroundStyle(DotsTheme.primaryText)
        }
    }

    private var uploadingOverlay: some View {
        ZStack {
            Color.black.opacity(0.56)
                .ignoresSafeArea()

            VStack(spacing: 14) {
                ProgressView()
                    .scaleEffect(1.3)
                    .tint(.white)

                Text("Uploading Floor Plan")
                    .font(.headline)
                    .foregroundStyle(DotsTheme.primaryText)

                Text("Please keep the app open.")
                    .font(.caption)
                    .foregroundStyle(DotsTheme.secondaryText)
                    .multilineTextAlignment(.center)
            }
            .padding(24)
            .frame(maxWidth: 300)
            .background(
                RoundedRectangle(cornerRadius: 16)
                    .fill(Color.black.opacity(0.92))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 16)
                    .stroke(DotsTheme.border, lineWidth: 1)
            )
        }
    }

    private var processingOverlay: some View {
        ZStack {
            Color.black.opacity(0.94)
                .ignoresSafeArea()

            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Button {
                        pollingTask?.cancel()
                        stopSimulatedAgentSequence()
                        uploadState = .idle
                    } label: {
                        ZStack {
                            Circle()
                                .fill(Color.white.opacity(0.08))
                                .frame(width: 36, height: 36)
                            Image(systemName: "chevron.left")
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundStyle(.white)
                        }
                    }

                    Spacer()

                    Text("Processing")
                        .font(.headline.weight(.medium))
                        .foregroundStyle(.white.opacity(0.82))

                    Spacer()

                    DotsWordmark(textSize: 22, dotDiameter: 5, weight: .semibold)
                }

                Text(processingContextLabel)
                    .font(.system(size: 16, weight: .medium, design: .monospaced))
                    .foregroundStyle(DotsTheme.tertiaryText)
                    .textCase(.uppercase)

                Text("Analyzing...")
                    .font(.system(size: 46, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)

                DotsRenderGrid(progress: visualProgress, tick: visualTick)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 4)

                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(agentStages.enumerated()), id: \.offset) { index, stage in
                        let stageState = agentStageState(for: index)
                        HStack(spacing: 8) {
                            agentStageIcon(for: index)
                            Text(stage)
                                .font(.system(size: 17, weight: .regular, design: .monospaced))
                                .foregroundStyle(agentStageTextColor(for: index))

                            Spacer()

                            if stageState == .active {
                                Text("...")
                                    .font(.system(size: 17, weight: .regular, design: .monospaced))
                                    .foregroundStyle(DotsTheme.secondaryText)
                            }
                        }
                    }
                }
                .padding(.top, 2)

                Spacer(minLength: 14)

                GeometryReader { proxy in
                    let totalWidth = proxy.size.width

                    ZStack {
                        RoundedRectangle(cornerRadius: 16)
                            .fill(Color.white.opacity(0.08))
                            .frame(height: 52)

                        RoundedRectangle(cornerRadius: 16)
                            .fill(Color.white.opacity(0.2))
                            .frame(width: max(58, totalWidth * visualProgress), height: 52)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .clipShape(RoundedRectangle(cornerRadius: 16))

                        Text("\(Int(visualProgress * 100))%")
                            .font(.system(size: 28, weight: .semibold, design: .rounded))
                            .foregroundStyle(.white.opacity(0.85))
                    }
                }
                .frame(height: 52)
            }
            .padding(.horizontal, 20)
            .padding(.top, 10)
            .padding(.bottom, 22)
        }
    }

    private enum AgentStageVisualState {
        case pending
        case active
        case done
    }

    private func agentStageState(for index: Int) -> AgentStageVisualState {
        if visualProgress >= 0.999 {
            return .done
        }
        if index < visualAgentIndex {
            return .done
        }
        if index == visualAgentIndex {
            return .active
        }
        return .pending
    }

    private func agentStageTextColor(for index: Int) -> Color {
        switch agentStageState(for: index) {
        case .done:
            return .white
        case .active:
            return .white.opacity(0.92)
        case .pending:
            return Color.white.opacity(0.45)
        }
    }

    @ViewBuilder
    private func agentStageIcon(for index: Int) -> some View {
        switch agentStageState(for: index) {
        case .done:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(Color.white)
        case .active:
            Circle()
                .stroke(Color.white, lineWidth: 1.4)
                .frame(width: 14, height: 14)
        case .pending:
            Image(systemName: "circle")
                .foregroundStyle(Color.white.opacity(0.35))
        }
    }

    private var processingContextLabel: String {
        let building = buildingName.trimmingCharacters(in: .whitespacesAndNewlines)
        let floor = floorName.trimmingCharacters(in: .whitespacesAndNewlines)

        if building.isEmpty && floor.isEmpty { return "BUILDING / FLOOR" }
        if floor.isEmpty { return building.uppercased() }
        if building.isEmpty { return floor.uppercased() }
        return "\(building.uppercased()) / \(floor.uppercased())"
    }

    private var canUpload: Bool {
        guard case .idle = uploadState else { return false }
        return selectedImage != nil
            && !buildingName.trimmingCharacters(in: .whitespaces).isEmpty
            && !floorName.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private var completedResult: (roomId: String, pdfUrl: String?, recommendationsUrl: String?)? {
        if case .complete(let roomId, let pdfUrl, _, let recommendationsUrl) = uploadState {
            return (roomId, pdfUrl, recommendationsUrl)
        }
        return nil
    }

    private var completedRoomId: String? {
        if case .complete(let roomId, _, _, _) = uploadState {
            return roomId
        }
        return nil
    }

    @MainActor
    private func startSimulatedAgentSequence() {
        visualAgentTask?.cancel()
        visualAgentIndex = 0
        visualProgress = 0.08
        visualTick = 0

        visualAgentTask = Task {
            var localProgress = 0.08
            var localTick = 0

            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 280_000_000)
                if Task.isCancelled { return }

                localTick += 1
                if localProgress < 0.93 {
                    let increment = 0.010 + Double((localTick % 5)) * 0.0024
                    localProgress = min(0.93, localProgress + increment)
                }

                let stageProgress = localProgress * Double(agentStages.count)
                let activeStage = min(agentStages.count - 1, max(0, Int(stageProgress.rounded(.down))))

                await MainActor.run {
                    visualTick = localTick
                    visualProgress = localProgress
                    visualAgentIndex = activeStage
                }
            }
        }
    }

    @MainActor
    private func finishSimulatedAgentSequence() {
        visualAgentIndex = max(0, agentStages.count - 1)
        visualProgress = 1.0
        visualAgentTask?.cancel()
        visualAgentTask = nil
    }

    @MainActor
    private func stopSimulatedAgentSequence() {
        visualAgentTask?.cancel()
        visualAgentTask = nil
        visualAgentIndex = 0
        visualProgress = 0
        visualTick = 0
    }

    // MARK: - Upload

    @MainActor
    private func performUpload() async {
        guard let image = selectedImage else { return }
        focusedField = nil
        hideKeyboard()
        uploadState = .uploading

        do {
            let response = try await BackendClient.shared.uploadFloorPlan(
                image: image,
                buildingName: buildingName.trimmingCharacters(in: .whitespaces),
                locationName: floorName.trimmingCharacters(in: .whitespaces)
            )
            let roomId = response.roomId
            pipelineStep = "Analyzing floor plan with AI..."
            uploadState = .processing(roomId: roomId)
            startSimulatedAgentSequence()

            pollingTask = Task {
                await pollUntilComplete(roomId: roomId)
            }
        } catch {
            stopSimulatedAgentSequence()
            uploadState = .failure(message: error.localizedDescription)
        }
    }

    @MainActor
    private func pollUntilComplete(roomId: String) async {
        let maxAttempts = 90

        for attempt in 1...maxAttempts {
            if Task.isCancelled { return }

            do {
                let status = try await BackendClient.shared.pollRoomStatus(roomId: roomId)

                switch status.status {
                case "received":
                    pipelineStep = "Uploading floor plan..."
                case "analyzing_floorplan":
                    pipelineStep = "Analyzing floor plan..."
                case "floorplan_analyzed", "pipeline_triggered", "enriched", "enriched_no_objects":
                    pipelineStep = "Generating ADA report, Braille PDF, and voice context..."
                default:
                    if status.status.starts(with: "error") {
                        stopSimulatedAgentSequence()
                        uploadState = .failure(message: "Pipeline error: \(status.status)")
                        return
                    }
                    pipelineStep = "Processing (\(status.status))..."
                }

                if status.isComplete {
                    finishSimulatedAgentSequence()
                    uploadState = .complete(
                        roomId: roomId,
                        pdfUrl: status.pdfUrl,
                        audioUrl: status.audioUrl,
                        recommendationsUrl: status.recommendationsPdfUrl
                    )
                    return
                }

                let map = status.statusMapDone
                let recommendations = status.statusRecommendationsDone
                let narration = status.statusNarrationDone
                let waiting = [
                    map ? nil : "Braille PDF",
                    recommendations ? nil : "ADA report",
                    narration ? nil : "voice context"
                ].compactMap { $0 }

                if !waiting.isEmpty {
                    pipelineStep = "Generating " + waiting.joined(separator: " + ") + "..."
                }
            } catch {
                if attempt > 5 {
                    pipelineStep = "Still working (retrying)..."
                }
            }

            try? await Task.sleep(nanoseconds: 3_000_000_000)
        }

        stopSimulatedAgentSequence()
        uploadState = .failure(message: "Pipeline timed out after about 4 minutes. Check backend status.")
    }
}

private struct DotsRenderGrid: View {
    let progress: Double
    let tick: Int

    private let rows = 7
    private let columns = 10

    var body: some View {
        let total = rows * columns
        let clampedProgress = max(0, min(1, progress))
        let settledCount = Int(Double(total) * clampedProgress)

        VStack(spacing: 9) {
            ForEach(0..<rows, id: \.self) { row in
                HStack(spacing: 13) {
                    ForEach(0..<columns, id: \.self) { col in
                        let index = row * columns + col
                        Circle()
                            .fill(Color.white.opacity(dotOpacity(index: index, settledCount: settledCount)))
                            .frame(width: 10, height: 10)
                    }
                }
            }
        }
        .padding(.vertical, 28)
        .padding(.horizontal, 26)
        .frame(maxWidth: .infinity)
        .background(
            RoundedRectangle(cornerRadius: 20)
                .fill(Color.white.opacity(0.02))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20)
                .stroke(Color.white.opacity(0.13), lineWidth: 1)
        )
    }

    private func dotOpacity(index: Int, settledCount: Int) -> Double {
        let total = rows * columns
        let blinkingIndex = (tick / 2) % max(1, total)

        if index == blinkingIndex {
            let blinkOn = (tick % 4) < 2
            return blinkOn ? 0.98 : 0.18
        }

        if index < settledCount {
            return 0.95
        }

        let shimmer = (index + tick * 11) % 21
        if shimmer == 0 || shimmer == 8 {
            return 0.35
        }

        return 0.08
    }
}

private extension View {
    func hideKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }
}

// MARK: - Report Hub

struct DotsReportHubView: View {
    let roomId: String
    let pdfUrl: String?
    let recommendationsUrl: String?
    let onTalkToAgent: () -> Void

    var body: some View {
        ZStack {
            DotsTheme.background.ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    DotsWordmark(textSize: 30, dotDiameter: 7, weight: .semibold)

                    Text("Review ADA feedback, open your Braille PDF, or talk with the voice agent.")
                        .font(.subheadline)
                        .foregroundStyle(DotsTheme.secondaryText)

                    Text("Room ID: \(roomId)")
                        .font(.caption.monospaced())
                        .foregroundStyle(DotsTheme.secondaryText)
                        .textSelection(.enabled)
                        .padding(.top, 2)

                    reportAction(
                        title: "ADA Feedback Report",
                        subtitle: "Open detailed compliance recommendations",
                        systemImage: "checklist",
                        urlString: recommendationsUrl
                    )

                    reportAction(
                        title: "Braille Map PDF",
                        subtitle: "Preview the generated tactile map",
                        systemImage: "doc.text",
                        urlString: pdfUrl
                    )

                    Button {
                        onTalkToAgent()
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "waveform.and.mic")
                                .font(.title3)
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Talk to Voice Agent")
                                    .font(.headline)
                                Text("Start a live conversation from the consumer perspective")
                                    .font(.caption)
                                    .foregroundStyle(DotsTheme.secondaryText)
                            }
                            Spacer()
                            Image(systemName: "chevron.right")
                                .foregroundStyle(DotsTheme.secondaryText)
                        }
                        .foregroundStyle(DotsTheme.primaryText)
                    }
                    .buttonStyle(DotsSecondaryButtonStyle())
                }
                .padding(20)
            }
        }
        .navigationTitle("Report")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .preferredColorScheme(.dark)
    }

    @ViewBuilder
    private func reportAction(title: String, subtitle: String, systemImage: String, urlString: String?) -> some View {
        if let urlString, !urlString.isEmpty {
            NavigationLink {
                DotsDocumentViewerView(title: title, rawUrl: urlString)
            } label: {
                HStack(spacing: 12) {
                    Image(systemName: systemImage)
                        .font(.title3)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(title)
                            .font(.headline)
                        Text(subtitle)
                            .font(.caption)
                            .foregroundStyle(DotsTheme.secondaryText)
                    }
                    Spacer()
                    Image(systemName: "chevron.right")
                        .foregroundStyle(DotsTheme.secondaryText)
                }
                .foregroundStyle(DotsTheme.primaryText)
            }
            .buttonStyle(DotsSecondaryButtonStyle())
        } else {
            HStack(spacing: 12) {
                Image(systemName: systemImage)
                    .font(.title3)
                    .foregroundStyle(DotsTheme.secondaryText)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(.headline)
                        .foregroundStyle(DotsTheme.secondaryText)
                    Text("Not available yet")
                        .font(.caption)
                        .foregroundStyle(DotsTheme.tertiaryText)
                }
                Spacer()
            }
            .padding(.vertical, 14)
            .padding(.horizontal, 16)
            .background(
                RoundedRectangle(cornerRadius: 15)
                    .fill(DotsTheme.panel)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 15)
                    .stroke(DotsTheme.border, lineWidth: 1)
            )
        }
    }
}

// MARK: - In-App Document Viewer

struct DotsDocumentViewerView: View {
    let title: String
    let rawUrl: String

    @Environment(\.openURL) private var openURL

    @State private var loadError: String?
    @State private var reloadToken = UUID()

    private var rewrittenURL: URL? {
        let rewritten = BackendClient.rewriteFileUrl(rawUrl)
        return URL(string: rewritten)
    }

    var body: some View {
        ZStack {
            DotsTheme.background.ignoresSafeArea()

            VStack(spacing: 12) {
                if let rewrittenURL {
                    DotsWebViewContainer(
                        url: rewrittenURL,
                        reloadToken: reloadToken,
                        loadError: $loadError
                    )
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .overlay(
                        RoundedRectangle(cornerRadius: 16)
                            .stroke(DotsTheme.border, lineWidth: 1)
                    )

                    if let loadError {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Could not load this document in-app")
                                .font(.headline)
                                .foregroundStyle(.red)

                            Text(loadError)
                                .font(.caption)
                                .foregroundStyle(DotsTheme.secondaryText)

                            Button("Retry") {
                                self.loadError = nil
                                reloadToken = UUID()
                            }
                            .buttonStyle(DotsSecondaryButtonStyle())
                        }
                        .dotsPanel()
                    }

                    HStack(spacing: 10) {
                        Button("Open in Browser") {
                            openURL(rewrittenURL)
                        }
                        .buttonStyle(DotsSecondaryButtonStyle())

                        Button("Reload") {
                            loadError = nil
                            reloadToken = UUID()
                        }
                        .buttonStyle(DotsSecondaryButtonStyle())
                    }
                } else {
                    VStack(spacing: 12) {
                        Text("Invalid document URL")
                            .font(.headline)
                            .foregroundStyle(.red)

                        Text("The report link is malformed. Try again from the report screen.")
                            .font(.caption)
                            .foregroundStyle(DotsTheme.secondaryText)
                            .multilineTextAlignment(.center)
                    }
                    .dotsPanel()
                }
            }
            .padding(20)
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .preferredColorScheme(.dark)
    }
}

struct DotsWebViewContainer: UIViewRepresentable {
    let url: URL
    let reloadToken: UUID
    @Binding var loadError: String?

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeUIView(context: Context) -> WKWebView {
        let webView = WKWebView(frame: .zero)
        webView.navigationDelegate = context.coordinator
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        let requestKey = url.absoluteString + reloadToken.uuidString
        guard context.coordinator.lastRequestKey != requestKey else { return }

        context.coordinator.lastRequestKey = requestKey
        uiView.load(URLRequest(url: url))
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var parent: DotsWebViewContainer
        var lastRequestKey = ""

        init(_ parent: DotsWebViewContainer) {
            self.parent = parent
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            parent.loadError = nil
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            parent.loadError = error.localizedDescription
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            parent.loadError = error.localizedDescription
        }
    }
}

// MARK: - Image Picker (PHPicker)

struct FloorPlanImagePicker: UIViewControllerRepresentable {
    @Binding var image: UIImage?
    @Environment(\.dismiss) private var dismiss

    func makeUIViewController(context: Context) -> PHPickerViewController {
        var config = PHPickerConfiguration()
        config.filter = .images
        config.selectionLimit = 1
        let picker = PHPickerViewController(configuration: config)
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ uiViewController: PHPickerViewController, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    class Coordinator: NSObject, PHPickerViewControllerDelegate {
        let parent: FloorPlanImagePicker

        init(_ parent: FloorPlanImagePicker) {
            self.parent = parent
        }

        func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
            parent.dismiss()

            guard let provider = results.first?.itemProvider,
                  provider.canLoadObject(ofClass: UIImage.self) else { return }

            provider.loadObject(ofClass: UIImage.self) { image, _ in
                DispatchQueue.main.async {
                    self.parent.image = image as? UIImage
                }
            }
        }
    }
}
