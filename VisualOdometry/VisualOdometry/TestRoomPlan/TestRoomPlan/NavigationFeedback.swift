import AVFoundation
import Foundation

final class NavigationSpeechEngine: NSObject, AVSpeechSynthesizerDelegate, @unchecked Sendable {
    private let synthesizer = AVSpeechSynthesizer()
    private var lastUtteranceText: String?
    private var lastUtteranceDate = Date.distantPast

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ text: String, interrupt: Bool = true, minimumInterval: TimeInterval = 1.0) {
        guard !text.isEmpty else { return }
        if lastUtteranceText == text, Date().timeIntervalSince(lastUtteranceDate) < minimumInterval {
            return
        }

        if interrupt, synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }

        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = 0.45
        utterance.pitchMultiplier = 1.1
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        synthesizer.speak(utterance)
        lastUtteranceText = text
        lastUtteranceDate = Date()
    }
}
