import SwiftUI

// MARK: - Conversation Message

struct ConversationMessage: Identifiable, Equatable {
    enum Role: Equatable { case assistant, user }
    let id = UUID()
    let role: Role
    let text: String
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
    let stepsTaken: Int
    let heading: Double
    let startingPoint: String
    let destinationName: String?
    let instruction: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Instruction
            Text(instruction)
                .font(.title3.weight(.semibold))
                .foregroundStyle(.white)

            // Route label: Starting Point → Destination
            HStack(spacing: 8) {
                HStack(spacing: 5) {
                    Image(systemName: "circle.fill")
                        .font(.system(size: 7))
                        .foregroundStyle(.green)
                    Text(startingPoint)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.white.opacity(0.85))
                }

                Image(systemName: "arrow.right")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.white.opacity(0.5))

                if let dest = destinationName {
                    HStack(spacing: 5) {
                        Image(systemName: "mappin.circle.fill")
                            .font(.system(size: 10))
                            .foregroundStyle(.yellow)
                        Text(dest)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.white.opacity(0.85))
                    }
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Color.white.opacity(0.06))
            .clipShape(Capsule())

            // Stats row
            HStack(spacing: 16) {
                statItem(title: "REMAINING", value: distanceRemaining)
                statItem(title: "WALKED", value: String(format: "%.1f m", distanceWalked))
                statItem(title: "STEPS", value: "\(stepsTaken)")

                Spacer()

                NavigationCompassView(heading: heading)
            }
        }
        .padding(16)
        .background(.black.opacity(0.84))
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(Color.white.opacity(0.14), lineWidth: 1)
        )
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

/// Conversational "How can I help?" panel shown after room alignment.
/// Handles voice/text input to determine destination, then transitions to active navigation.
struct NavigationAssistantView: View {
    @Binding var messages: [ConversationMessage]
    let destinationNames: [String]
    let onDestinationChosen: (String) -> Void
    let onStopNavigation: () -> Void
    let isNavigationActive: Bool

    @StateObject private var speechManager = SpeechRecognitionManager()
    @State private var textInput: String = ""
    @State private var hasGreeted: Bool = false

    private let speechEngine = NavigationSpeechEngine()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Conversation transcript
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
                    .frame(maxHeight: 160)
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
                // Input area
                HStack(spacing: 10) {
                    TextField("e.g. Take me to the bathroom", text: $textInput)
                        .textFieldStyle(.plain)
                        .font(.system(size: 16, design: .rounded))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 12)
                        .background(
                            RoundedRectangle(cornerRadius: 14)
                                .fill(Color.white.opacity(0.08))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 14)
                                .stroke(Color.white.opacity(0.15), lineWidth: 1)
                        )
                        .submitLabel(.send)
                        .onSubmit { handleTextSubmit() }

                    // Mic button
                    Button {
                        toggleSpeechInput()
                    } label: {
                        Image(systemName: speechManager.isListening ? "mic.fill" : "mic")
                            .font(.system(size: 20, weight: .semibold))
                            .foregroundStyle(speechManager.isListening ? .black : .white)
                            .frame(width: 48, height: 48)
                            .background(
                                Circle().fill(speechManager.isListening ? Color.white : Color.white.opacity(0.1))
                            )
                            .overlay(
                                Circle().stroke(Color.white.opacity(0.2), lineWidth: 1)
                            )
                    }
                    .accessibilityLabel(speechManager.isListening ? "Stop listening" : "Start listening")

                    // Send button
                    Button {
                        handleTextSubmit()
                    } label: {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 32))
                            .foregroundStyle(.white.opacity(textInput.isEmpty ? 0.3 : 0.9))
                    }
                    .disabled(textInput.trimmingCharacters(in: .whitespaces).isEmpty)
                }

                // Quick destination chips
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
        }
        .onChange(of: speechManager.transcribedText) { _, newText in
            if !newText.isEmpty {
                textInput = newText
            }
        }
    }

    // MARK: - Actions

    private func greetUser() {
        guard !hasGreeted else { return }
        hasGreeted = true
        let greeting = "How can I help you today? Tell me where you'd like to go."
        messages.append(ConversationMessage(role: .assistant, text: greeting))
        speechEngine.speak(greeting)
    }

    private func handleTextSubmit() {
        let text = textInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }

        messages.append(ConversationMessage(role: .user, text: text))
        textInput = ""
        speechManager.stopListening()

        parseAndNavigate(text)
    }

    private func toggleSpeechInput() {
        if speechManager.isListening {
            speechManager.stopListening()
            // If we got text, submit it
            let text = speechManager.transcribedText.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty {
                textInput = text
                handleTextSubmit()
            }
        } else {
            speechManager.resetTranscription()
            Task {
                let authorized = await speechManager.requestAuthorization()
                if authorized {
                    speechManager.startListening()
                } else {
                    messages.append(ConversationMessage(
                        role: .assistant,
                        text: "I need microphone access for voice input. You can type your destination instead."
                    ))
                }
            }
        }
    }

    private func chooseDestination(_ name: String) {
        messages.append(ConversationMessage(role: .user, text: "Take me to \(name)"))
        let response = "I'll guide you to \(name). Walk forward and I'll give you directions."
        messages.append(ConversationMessage(role: .assistant, text: response))
        speechEngine.speak(response)
        onDestinationChosen(name)
    }

    private func parseAndNavigate(_ text: String) {
        // Try to match against known destinations
        let lowerText = text.lowercased()

        // Check each destination name for a fuzzy match
        for name in destinationNames {
            let lowerName = name.lowercased()
            if lowerText.contains(lowerName) {
                chooseDestinationFromParse(name)
                return
            }
        }

        // Try common synonyms
        let synonymMap: [(keywords: [String], destination: String)] = [
            (["bathroom", "restroom", "toilet", "washroom", "bath"], "Bathroom"),
            (["bed", "bedroom", "sleep"], "Bed"),
            (["seat", "chair", "table", "sit", "seating", "lounge", "dining"], "Seating Area"),
            (["exit", "door", "entrance", "entry", "front door", "out", "leave"], "Exit"),
        ]

        for mapping in synonymMap {
            if mapping.keywords.contains(where: { lowerText.contains($0) }) {
                if destinationNames.contains(mapping.destination) {
                    chooseDestinationFromParse(mapping.destination)
                    return
                }
            }
        }

        // No match found
        let available = destinationNames.joined(separator: ", ")
        let response = "I couldn't find that destination. Available places are: \(available). Where would you like to go?"
        messages.append(ConversationMessage(role: .assistant, text: response))
        speechEngine.speak(response)
    }

    private func chooseDestinationFromParse(_ name: String) {
        let response = "I'll guide you to \(name). Walk forward and I'll give you directions."
        messages.append(ConversationMessage(role: .assistant, text: response))
        speechEngine.speak(response)
        onDestinationChosen(name)
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
