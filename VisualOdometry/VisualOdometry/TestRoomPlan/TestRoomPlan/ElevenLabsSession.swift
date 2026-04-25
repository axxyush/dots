import AVFoundation
import Combine
import Foundation
import SwiftUI

#if canImport(ElevenLabs)
import ElevenLabs
#endif

// MARK: - Session State

enum VoiceSessionState: Equatable {
    case idle
    case requestingPermission
    case fetchingToken
    case connecting
    case listening
    case speaking
    case ended
    case error(String)

    var isLive: Bool {
        switch self {
        case .listening, .speaking, .connecting: return true
        default: return false
        }
    }

    var statusLabel: String {
        switch self {
        case .idle: return "Ready"
        case .requestingPermission: return "Requesting microphone access…"
        case .fetchingToken: return "Connecting to your guide…"
        case .connecting: return "Connecting…"
        case .listening: return "Listening"
        case .speaking: return "Speaking"
        case .ended: return "Conversation ended"
        case .error(let m): return "Error: \(m)"
        }
    }
}

struct TranscriptLine: Identifiable, Equatable {
    enum Speaker { case user, agent }
    let id = UUID()
    let speaker: Speaker
    var text: String
}

// MARK: - ElevenLabs Session

/// Owns the lifetime of a single ElevenLabs Conversational AI session.
///
/// The SDK uses LiveKit under the hood and manages its own audio session +
/// echo cancellation, so this wrapper only needs to: (1) request mic permission,
/// (2) ask the backend for a per-room token + overrides, (3) call
/// `ElevenLabs.startConversation`, and (4) republish state for SwiftUI.
@MainActor
final class ElevenLabsSession: ObservableObject {

    @Published var state: VoiceSessionState = .idle
    @Published var transcript: [TranscriptLine] = []
    @Published var isMicPaused = false

    #if canImport(ElevenLabs)
    private var conversation: Conversation?
    private var stateCancellable: AnyCancellable?
    private var messagesCancellable: AnyCancellable?
    private var agentStateCancellable: AnyCancellable?
    private var muteCancellable: AnyCancellable?
    #endif

    /// Fetches a token and starts the conversation. Safe to call once per session.
    func start(roomId: String) async {
        guard case .idle = state else { return }

        // 1. Microphone permission
        state = .requestingPermission
        let granted = await Self.requestMicrophonePermission()
        guard granted else {
            state = .error("Microphone permission denied. Open Settings to enable it.")
            return
        }

        // 2. Token + overrides from the Dots backend
        state = .fetchingToken
        let session: VoiceSessionResponse
        do {
            session = try await BackendClient.shared.startVoiceSession(roomId: roomId)
        } catch {
            state = .error(error.localizedDescription)
            return
        }

        // 3. Hand off to the ElevenLabs SDK
        state = .connecting
        #if canImport(ElevenLabs)
        do {
            let agentOverrides = AgentOverrides(
                prompt: session.agentOverrides.prompt,
                firstMessage: session.agentOverrides.firstMessage,
                language: session.agentOverrides.language.flatMap(Language.init(rawValue:))
            )
            let ttsOverrides = TTSOverrides(voiceId: session.ttsOverrides.voiceId)

            let config = ConversationConfig(
                agentOverrides: agentOverrides,
                ttsOverrides: ttsOverrides
            )

            let convo = try await ElevenLabs.startConversation(
                conversationToken: session.conversationToken,
                config: config
            )
            self.conversation = convo
            self.isMicPaused = convo.isMuted
            self.bind(to: convo)
        } catch {
            state = .error("Voice agent failed to start: \(error.localizedDescription)")
        }
        #else
        state = .error("ElevenLabs SDK not linked. Add the elevenlabs-swift-sdk Swift Package to the TestRoomPlan target.")
        #endif
    }

    func end() {
        #if canImport(ElevenLabs)
        Task { [conversation] in
            await conversation?.endConversation()
        }
        stateCancellable = nil
        messagesCancellable = nil
        agentStateCancellable = nil
        muteCancellable = nil
        conversation = nil
        #endif
        isMicPaused = false
        if state != .ended {
            state = .ended
        }
    }

    func toggleMicPause() {
        #if canImport(ElevenLabs)
        guard let conversation else { return }
        let target = !conversation.isMuted
        Task {
            do {
                try await conversation.setMuted(target)
                await MainActor.run {
                    self.isMicPaused = target
                }
            } catch {
                await MainActor.run {
                    self.state = .error("Could not toggle microphone: \(error.localizedDescription)")
                }
            }
        }
        #else
        isMicPaused.toggle()
        #endif
    }

    deinit {
        #if canImport(ElevenLabs)
        let convo = conversation
        Task { @MainActor in
            await convo?.endConversation()
        }
        #endif
    }

    // MARK: - SDK bindings

    #if canImport(ElevenLabs)
    private func bind(to convo: Conversation) {
        stateCancellable = convo.$state
            .receive(on: DispatchQueue.main)
            .sink { [weak self] sdkState in
                self?.applySDKState(sdkState)
            }

        messagesCancellable = convo.$messages
            .receive(on: DispatchQueue.main)
            .sink { [weak self] msgs in
                self?.applyMessages(msgs)
            }

        agentStateCancellable = convo.$agentState
            .receive(on: DispatchQueue.main)
            .sink { [weak self] agentState in
                self?.applyAgentState(agentState)
            }

        muteCancellable = convo.$isMuted
            .receive(on: DispatchQueue.main)
            .sink { [weak self] muted in
                self?.isMicPaused = muted
            }
    }

    private func applySDKState(_ sdkState: ConversationState) {
        switch sdkState {
        case .idle:
            // Keep our intermediate states (.connecting/.fetchingToken) visible.
            break
        case .connecting:
            state = .connecting
        case .active:
            // Default to listening once active; agentState updates flip to .speaking.
            if state != .listening && state != .speaking {
                state = .listening
            }
        case .ended:
            state = .ended
        case .error(let err):
            state = .error(String(describing: err))
        }
    }

    private func applyAgentState(_ agentState: ElevenLabs.AgentState) {
        guard case .active = conversation?.state ?? .idle else { return }
        switch agentState {
        case .speaking:
            state = .speaking
        case .listening:
            state = .listening
        default:
            // Other internal states (thinking, etc.) — keep current.
            break
        }
    }

    private func applyMessages(_ messages: [Message]) {
        transcript = messages.map { msg in
            TranscriptLine(
                speaker: msg.role == .user ? .user : .agent,
                text: msg.content
            )
        }
    }
    #endif

    // MARK: - Permissions

    private static func requestMicrophonePermission() async -> Bool {
        if #available(iOS 17.0, *) {
            return await AVAudioApplication.requestRecordPermission()
        } else {
            return await withCheckedContinuation { cont in
                AVAudioSession.sharedInstance().requestRecordPermission { granted in
                    cont.resume(returning: granted)
                }
            }
        }
    }
}
