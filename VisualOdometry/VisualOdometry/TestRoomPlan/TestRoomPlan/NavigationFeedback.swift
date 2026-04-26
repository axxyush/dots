import AVFoundation
import Foundation

final class NavigationSpeechEngine: NSObject, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate {
    static let shared = NavigationSpeechEngine()

    private let synthesizer = AVSpeechSynthesizer()
    private let urlSession = URLSession(configuration: .ephemeral)
    private var audioPlayer: AVAudioPlayer?
    private var pendingTask: Task<Void, Never>?
    private var lastUtteranceText: String?
    private var lastUtteranceDate = Date.distantPast

    private override init() {
        super.init()
        synthesizer.delegate = self
        configureAudioSession()
    }

    func speak(_ text: String, interrupt: Bool = true, minimumInterval: TimeInterval = 1.0) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if lastUtteranceText == trimmed, Date().timeIntervalSince(lastUtteranceDate) < minimumInterval {
            return
        }

        if interrupt {
            stopSpeaking()
        } else if isSpeaking {
            return
        }

        lastUtteranceText = trimmed
        lastUtteranceDate = Date()
        configureAudioSession()

        if isElevenLabsConfigured {
            let utterance = trimmed
            pendingTask = Task { [weak self] in
                await self?.speakWithElevenLabs(utterance)
            }
        } else {
            speakWithSystemVoice(trimmed)
        }
    }

    func stopSpeaking() {
        pendingTask?.cancel()
        pendingTask = nil

        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }

        audioPlayer?.stop()
        audioPlayer = nil
    }

    private var isSpeaking: Bool {
        synthesizer.isSpeaking || (audioPlayer?.isPlaying ?? false) || pendingTask != nil
    }

    private var isElevenLabsConfigured: Bool {
        !AppSecrets.elevenLabsApiKey.isEmpty && !AppSecrets.elevenLabsVoiceId.isEmpty
    }

    private func configureAudioSession() {
        let audioSession = AVAudioSession.sharedInstance()
        try? audioSession.setCategory(
            .playAndRecord,
            mode: .default,
            options: [.mixWithOthers, .defaultToSpeaker, .allowBluetoothHFP]
        )
        try? audioSession.setActive(true, options: .notifyOthersOnDeactivation)
    }

    private func speakWithSystemVoice(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = 0.48
        utterance.pitchMultiplier = 1.05
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.preUtteranceDelay = 0.05
        synthesizer.speak(utterance)
    }

    private func speakWithElevenLabs(_ text: String) async {
        do {
            let data = try await requestElevenLabsAudio(for: text)
            guard !Task.isCancelled else { return }

            let player = try AVAudioPlayer(data: data)
            player.delegate = self
            player.prepareToPlay()
            audioPlayer = player
            pendingTask = nil
            player.play()
        } catch {
            pendingTask = nil
            speakWithSystemVoice(text)
        }
    }

    private func requestElevenLabsAudio(for text: String) async throws -> Data {
        let voiceID = AppSecrets.elevenLabsVoiceId
        let baseURL = "https://api.elevenlabs.io/v1/text-to-speech/\(voiceID)"
        guard var components = URLComponents(string: baseURL) else {
            throw URLError(.badURL)
        }
        components.queryItems = [
            URLQueryItem(name: "output_format", value: "mp3_44100_128"),
            URLQueryItem(name: "optimize_streaming_latency", value: "3")
        ]
        guard let url = components.url else {
            throw URLError(.badURL)
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(AppSecrets.elevenLabsApiKey, forHTTPHeaderField: "xi-api-key")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body = ElevenLabsSpeechRequest(
            text: text,
            modelID: "eleven_flash_v2_5",
            languageCode: "en",
            voiceSettings: ElevenLabsVoiceSettings(
                stability: 0.45,
                similarityBoost: 0.8,
                style: 0.2,
                useSpeakerBoost: true
            )
        )
        request.httpBody = try JSONEncoder().encode(body)

        let (data, response) = try await urlSession.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        guard (200...299).contains(httpResponse.statusCode), !data.isEmpty else {
            throw URLError(.cannotDecodeRawData)
        }
        return data
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        if audioPlayer === player {
            audioPlayer = nil
        }
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        if audioPlayer === player {
            audioPlayer = nil
        }
    }
}

private struct ElevenLabsSpeechRequest: Encodable {
    let text: String
    let modelID: String
    let languageCode: String
    let voiceSettings: ElevenLabsVoiceSettings

    enum CodingKeys: String, CodingKey {
        case text
        case modelID = "model_id"
        case languageCode = "language_code"
        case voiceSettings = "voice_settings"
    }
}

private struct ElevenLabsVoiceSettings: Encodable {
    let stability: Float
    let similarityBoost: Float
    let style: Float
    let useSpeakerBoost: Bool

    enum CodingKeys: String, CodingKey {
        case stability
        case similarityBoost = "similarity_boost"
        case style
        case useSpeakerBoost = "use_speaker_boost"
    }
}
