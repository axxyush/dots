import AudioToolbox
import Speech
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
            Text(instruction)
                .font(.callout.weight(.semibold))
                .foregroundStyle(.white)
                .accessibilityLabel(instruction)

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

// MARK: - Navigation Assistant View

/// Live navigation assistant that uses push-to-talk speech capture.
struct NavigationAssistantView: View {
    @Binding var messages: [ConversationMessage]
    let destinationNames: [String]
    let roomContext: String
    let roomContextJSON: String
    let onNavigationRequested: (_ sourceName: String?, _ destinationName: String) -> NavigationRequestResult
    let onStopNavigation: () -> Void
    let isNavigationActive: Bool

    @StateObject private var wakeWordListener = WakeWordListener()
    private let intentRouter = ZeticIntentRouter()
    @State private var isProcessing = false

    private let speechEngine = NavigationSpeechEngine.shared

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
                // Voice status indicator + mic button
                voiceStatusBar

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
            configureVoiceInput()
        }
        .onDisappear {
            wakeWordListener.stopEverything()
            speechEngine.stopSpeaking()
        }
    }

    // MARK: - Voice Status Bar (includes mic button)

    private var voiceStatusBar: some View {
        HStack(spacing: 12) {
            // Status info
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
                } else if let errorText = wakeWordListener.errorMessage {
                    Text(errorText)
                        .font(.caption)
                        .foregroundStyle(.orange.opacity(0.9))
                        .lineLimit(2)
                } else {
                    Text("Tap the mic to ask for a route or a room question")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.5))
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(micAccessibilityLabel)

            Spacer()

            // Manual mic button — always available as fallback
            Button {
                micButtonTapped()
            } label: {
                ZStack {
                    Circle()
                        .fill(micButtonColor.opacity(0.15))
                        .frame(width: 52, height: 52)

                    // Pulsing ring when actively recording
                    if wakeWordListener.isCommandActive {
                        Circle()
                            .stroke(Color.green.opacity(0.5), lineWidth: 2)
                            .frame(width: 52, height: 52)
                            .scaleEffect(1.3)
                            .opacity(0.6)
                            .animation(
                                .easeInOut(duration: 0.8).repeatForever(autoreverses: true),
                                value: wakeWordListener.isCommandActive
                            )
                    }

                    Image(systemName: micIconName)
                        .font(.system(size: 22, weight: .semibold))
                        .foregroundStyle(micButtonColor)
                }
            }
            .accessibilityLabel(wakeWordListener.isCommandActive ? "Stop recording" : "Start recording")
            .accessibilityHint("Tap to speak a command")
        }
        .padding(.vertical, 4)
    }

    // MARK: - Mic Button

    private func micButtonTapped() {
        if wakeWordListener.isCommandActive {
            wakeWordListener.finishCommandCapture()
            return
        }

        Task { @MainActor in
            speechEngine.stopSpeaking()
            let started = await wakeWordListener.activateManually()
            if !started, let errorText = wakeWordListener.errorMessage {
                messages.append(ConversationMessage(role: .assistant, text: errorText))
            }
        }
    }

    private var micButtonColor: Color {
        if wakeWordListener.isCommandActive { return .green }
        return .white
    }

    private var micIconName: String {
        if wakeWordListener.isCommandActive { return "waveform" }
        return "mic.fill"
    }

    private var statusText: String {
        if isProcessing { return "Thinking…" }
        if wakeWordListener.isCommandActive { return "Listening…" }
        if wakeWordListener.errorMessage != nil { return "Microphone Needed" }
        return "Tap To Talk"
    }

    private var micAccessibilityLabel: String {
        if wakeWordListener.isCommandActive { return "Recording your command" }
        return "Tap the microphone to ask for a route or room information"
    }

    // MARK: - Setup

    private func greetUser() {
        let greeting = "How can I help you? Tap the mic to ask for directions or ask about this saved room."
        messages.append(ConversationMessage(role: .assistant, text: greeting))
        speechEngine.speak(greeting)
    }

    private func configureVoiceInput() {
        wakeWordListener.onWakeWord = {
            AudioServicesPlaySystemSound(1113) // Tink sound
        }

        wakeWordListener.onCommandReady = { command in
            handleVoiceCommand(command)
        }
    }

    // MARK: - Command Processing

    private func handleVoiceCommand(_ text: String) {
        messages.append(ConversationMessage(role: .user, text: text))
        isProcessing = true

        if let route = parseRouteRequest(from: text) {
            isProcessing = false
            sendNavigationRequest(sourceName: route.sourceName, destinationName: route.destinationName)
            return
        }

        Task {
            let result = await intentRouter.resolveIntent(
                userText: text,
                destinations: destinationNames,
                roomContext: roomContext,
                roomContextJSON: roomContextJSON
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
                    // Try raw text as destination name directly
                    sendNavigationRequest(sourceName: nil, destinationName: text)
                }
            }
        }
    }

    private func chooseDestination(_ name: String) {
        wakeWordListener.stopEverything()
        messages.append(ConversationMessage(role: .user, text: "Take me to \(name)"))
        sendNavigationRequest(sourceName: nil, destinationName: name)
    }

    private func parseRouteRequest(from text: String) -> (sourceName: String, destinationName: String)? {
        let lowered = text.lowercased()

        if let route = parseRoutePattern(in: text, pattern: #"(?i)\bfrom\s+(.+?)\s+to\s+(.+)$"#) {
            return route
        }

        if let route = parseRoutePattern(in: text, pattern: #"(?i)^(.+?)\s*->\s*(.+)$"#) {
            return route
        }

        if lowered.contains(" to "), let route = parseLooseToRoute(in: text) {
            return route
        }

        return nil
    }

    private func parseRoutePattern(in text: String, pattern: String) -> (sourceName: String, destinationName: String)? {
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return nil }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        guard let match = regex.firstMatch(in: text, options: [], range: range),
              match.numberOfRanges == 3,
              let sourceRange = Range(match.range(at: 1), in: text),
              let destinationRange = Range(match.range(at: 2), in: text)
        else {
            return nil
        }

        let sourceFragment = String(text[sourceRange])
        let destinationFragment = String(text[destinationRange])
        guard
            let sourceName = bestMatchingDestination(in: sourceFragment),
            let destinationName = bestMatchingDestination(in: destinationFragment),
            sourceName != destinationName
        else {
            return nil
        }

        return (sourceName, destinationName)
    }

    private func parseLooseToRoute(in text: String) -> (sourceName: String, destinationName: String)? {
        guard let toRange = text.range(of: " to ", options: [.caseInsensitive]) else { return nil }

        let sourceFragment = String(text[..<toRange.lowerBound])
        let destinationFragment = String(text[toRange.upperBound...])

        guard
            let sourceName = bestMatchingDestination(in: sourceFragment),
            let destinationName = bestMatchingDestination(in: destinationFragment),
            sourceName != destinationName
        else {
            return nil
        }

        return (sourceName, destinationName)
    }

    private func bestMatchingDestination(in fragment: String) -> String? {
        let lowered = fragment.lowercased()

        if let exact = destinationNames.first(where: { lowered.contains($0.lowercased()) }) {
            return exact
        }

        if lowered.contains("door") {
            let digits = fragment.filter(\.isNumber)
            if let doorNumber = Int(digits), doorNumber > 0 {
                return destinationNames.first { candidate in
                    candidate.lowercased().contains("door") &&
                    candidate.contains("\(doorNumber)")
                }
            }
        }

        return nil
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
