import AudioToolbox
import SwiftUI

// MARK: - Conversation Message

struct ConversationMessage: Identifiable, Equatable {
    enum Role: Equatable { case assistant, user }
    let id = UUID()
    let role: Role
    let text: String
}

struct NavigationRequestResult: Equatable {
    let didStart: Bool
    let response: String
}

// MARK: - Compass View

struct NavigationCompassView: View {
    let heading: Double

    var body: some View {
        VStack(spacing: 4) {
            ZStack {
                Circle()
                    .stroke(lineWidth: 3)
                    .foregroundColor(.secondary.opacity(0.3))
                    .frame(width: 44, height: 44)

                Image(systemName: "location.north.fill")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 16, height: 16)
                    .foregroundColor(.red)
                    .rotationEffect(.degrees(-heading))
            }

            Text("\(Int(heading))° \(CompassUtilities.directionString(for: heading))")
                .font(.system(size: 10, weight: .bold))
                .foregroundColor(.secondary)
        }
    }
}

// MARK: - Navigation Stats HUD

struct NavigationStatsHUD: View {
    let distanceRemaining: String
    let distanceWalked: Float
    let heading: Double
    let startingPoint: String
    let destinationName: String?
    let instruction: String
    let facingText: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Instruction
            Text(instruction)
                .font(.callout.weight(.semibold))
                .foregroundStyle(.white)
                .accessibilityLabel(instruction)

            // Route label: Starting Point → Destination
            HStack(spacing: 6) {
                Image(systemName: "circle.fill")
                    .font(.system(size: 6))
                    .foregroundStyle(.green)
                Text(startingPoint)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.white.opacity(0.85))

                Image(systemName: "arrow.right")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(.white.opacity(0.5))

                if let dest = destinationName {
                    Image(systemName: "mappin.circle.fill")
                        .font(.system(size: 8))
                        .foregroundStyle(.yellow)
                    Text(dest)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(.white.opacity(0.85))
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Route from \(startingPoint) to \(destinationName ?? "destination")")

            HStack(spacing: 12) {
                statItem(title: "LEFT", value: distanceRemaining)
                statItem(title: "WALKED", value: String(format: "%.1f m", distanceWalked))
                statItem(title: "FACING", value: facingText)
                Spacer()
                NavigationCompassView(heading: heading)
            }
        }
    }

    private func statItem(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.white.opacity(0.55))
            Text(value)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.white)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Color.white.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

// MARK: - Navigation Assistant View (Voice-First)

/// Voice-first navigation assistant. Listens for "Hey Dots" wake word,
/// captures commands, and auto-submits after 2 seconds of silence.
/// Keeps a visible transcript for sighted helpers and quick-tap destination chips.
struct NavigationAssistantView: View {
    @Binding var messages: [ConversationMessage]
    let destinationNames: [String]
    let roomContext: String
    let onNavigationRequested: (_ sourceName: String?, _ destinationName: String) -> NavigationRequestResult
    let onStopNavigation: () -> Void
    let isNavigationActive: Bool

    @StateObject private var wakeWordListener = WakeWordListener()
    @StateObject private var intentRouter = ZeticIntentRouter()
    @State private var isProcessing = false

    private let speechEngine = NavigationSpeechEngine()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Transcript (read-only, for sighted helpers)
            if !messages.isEmpty {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(messages) { msg in
                                assistantBubble(msg)
                                    .id(msg.id)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                    .frame(maxHeight: 140)
                    .onChange(of: messages.count) { _, _ in
                        if let last = messages.last {
                            withAnimation(.easeOut(duration: 0.2)) {
                                proxy.scrollTo(last.id, anchor: .bottom)
                            }
                        }
                    }
                }
            }

            if !isNavigationActive {
                // Voice status indicator
                voiceStatusIndicator

                // Quick destination chips (accessible via VoiceOver tap)
                if !destinationNames.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(destinationNames, id: \.self) { name in
                                Button {
                                    chooseDestination(name)
                                } label: {
                                    Text(name)
                                        .font(.caption.weight(.medium))
                                        .foregroundStyle(.white)
                                        .padding(.horizontal, 14)
                                        .padding(.vertical, 8)
                                        .background(
                                            Capsule().fill(Color.white.opacity(0.1))
                                        )
                                        .overlay(
                                            Capsule().stroke(Color.white.opacity(0.2), lineWidth: 1)
                                        )
                                }
                                .accessibilityLabel("Navigate to \(name)")
                            }
                        }
                    }
                }
            } else {
                Button("Stop Navigation") {
                    onStopNavigation()
                }
                .frame(maxWidth: .infinity)
                .buttonStyle(DotsSecondaryButtonStyle())
                .accessibilityHint("Stops spoken and visual guidance.")
            }
        }
        .padding(16)
        .background(.black.opacity(0.84))
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(Color.white.opacity(0.14), lineWidth: 1)
        )
        .onAppear {
            greetUser()
            Task { await intentRouter.loadModel() }
            setupWakeWord()
        }
        .onDisappear {
            wakeWordListener.stopEverything()
        }
    }

    // MARK: - Voice Status Indicator

    private var voiceStatusIndicator: some View {
        HStack(spacing: 12) {
            // Animated mic indicator
            ZStack {
                Circle()
                    .fill(micColor.opacity(0.15))
                    .frame(width: 48, height: 48)

                Circle()
                    .fill(micColor.opacity(wakeWordListener.isCommandActive ? 0.3 : 0))
                    .frame(width: 48, height: 48)
                    .scaleEffect(wakeWordListener.isCommandActive ? 1.4 : 1.0)
                    .animation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true), value: wakeWordListener.isCommandActive)

                Image(systemName: micIconName)
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(micColor)
            }
            .accessibilityLabel(micAccessibilityLabel)

            VStack(alignment: .leading, spacing: 2) {
                Text(statusText)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.white)

                if wakeWordListener.isCommandActive && !wakeWordListener.commandText.isEmpty {
                    Text(wakeWordListener.commandText)
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.7))
                        .lineLimit(2)
                } else if isProcessing {
                    Text("Processing…")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.5))
                } else {
                    Text("Say \"Hey Dots\" to start")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.5))
                }
            }

            Spacer()
        }
        .padding(.vertical, 4)
    }

    private var micColor: Color {
        if wakeWordListener.isCommandActive { return .green }
        if wakeWordListener.isPassiveListening { return .white }
        return .gray
    }

    private var micIconName: String {
        if wakeWordListener.isCommandActive { return "waveform" }
        if wakeWordListener.isPassiveListening { return "mic.fill" }
        return "mic.slash"
    }

    private var statusText: String {
        if isProcessing { return "Thinking…" }
        if wakeWordListener.isCommandActive { return "Listening…" }
        if wakeWordListener.isPassiveListening { return "Ready" }
        return "Starting…"
    }

    private var micAccessibilityLabel: String {
        if wakeWordListener.isCommandActive { return "Recording your command" }
        if wakeWordListener.isPassiveListening { return "Listening for Hey Dots" }
        return "Microphone inactive"
    }

    // MARK: - Setup

    private func greetUser() {
        let greeting = "How can I help you? Say Hey Dots, then tell me where you'd like to go."
        messages.append(ConversationMessage(role: .assistant, text: greeting))
        speechEngine.speak(greeting)
    }

    private func setupWakeWord() {
        wakeWordListener.onWakeWord = { [self] in
            // Play a brief auditory cue that we're listening
            AudioServicesPlaySystemSound(1113) // Tink sound
        }

        wakeWordListener.onCommandReady = { [self] command in
            handleVoiceCommand(command)
        }

        // Start passive listening after greeting finishes
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            Task {
                let authorized = await SpeechRecognitionManager().requestAuthorization()
                if authorized {
                    wakeWordListener.startPassiveListening()
                } else {
                    messages.append(ConversationMessage(
                        role: .assistant,
                        text: "I need microphone access to listen for your commands. Please enable it in Settings."
                    ))
                }
            }
        }
    }

    // MARK: - Command Processing

    private func handleVoiceCommand(_ text: String) {
        messages.append(ConversationMessage(role: .user, text: text))
        isProcessing = true

        // Pause listening while we process
        wakeWordListener.pauseListening()

        Task {
            let result = await intentRouter.resolveIntent(
                userText: text,
                destinations: destinationNames,
                roomContext: roomContext
            )

            await MainActor.run {
                isProcessing = false

                switch result {
                case .navigate(let destinationName):
                    sendNavigationRequest(sourceName: nil, destinationName: destinationName)

                case .answer(let answerText):
                    messages.append(ConversationMessage(role: .assistant, text: answerText))
                    speechEngine.speak(answerText)

                case .unknown:
                    // Try raw text as destination name
                    sendNavigationRequest(sourceName: nil, destinationName: text)
                }
            }
        }
    }

    private func chooseDestination(_ name: String) {
        messages.append(ConversationMessage(role: .user, text: "Take me to \(name)"))
        sendNavigationRequest(sourceName: nil, destinationName: name)
    }

    private func sendNavigationRequest(sourceName: String?, destinationName: String) {
        let result = onNavigationRequested(sourceName, destinationName)
        var response = result.response

        if !result.didStart,
           !destinationNames.isEmpty,
           !response.contains("Available places"),
           response.lowercased().contains("could not find") {
            let available = destinationNames.joined(separator: ", ")
            response += " Available places are: \(available)."
        }

        messages.append(ConversationMessage(role: .assistant, text: response))
        speechEngine.speak(response)
    }

    // MARK: - Bubble

    @ViewBuilder
    private func assistantBubble(_ msg: ConversationMessage) -> some View {
        HStack(alignment: .top) {
            if msg.role == .user { Spacer(minLength: 40) }

            Text(msg.text)
                .font(.system(size: 15, weight: .regular, design: .rounded))
                .foregroundStyle(msg.role == .user ? .black : .white)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 14)
                        .fill(msg.role == .user ? Color.white : Color.white.opacity(0.1))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 14)
                        .stroke(msg.role == .user ? Color.white.opacity(0.25) : Color.white.opacity(0.12), lineWidth: 1)
                )

            if msg.role == .assistant { Spacer(minLength: 40) }
        }
    }
}
