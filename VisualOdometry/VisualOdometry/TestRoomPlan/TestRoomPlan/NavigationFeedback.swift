import AVFoundation
import CoreHaptics
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

final class NavigationHapticsEngine {
    private var engine: CHHapticEngine?

    init() {
        prepareEngine()
    }

    func playLeftTurn() {
        playTransients(at: [0, 0.18])
    }

    func playRightTurn() {
        playTransients(at: [0, 0.18, 0.36])
    }

    func playArrival() {
        guard CHHapticEngine.capabilitiesForHardware().supportsHaptics else { return }
        let intensity = CHHapticEventParameter(parameterID: .hapticIntensity, value: 1.0)
        let sharpness = CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.4)
        let event = CHHapticEvent(
            eventType: .hapticContinuous,
            parameters: [intensity, sharpness],
            relativeTime: 0,
            duration: 0.8
        )
        play(events: [event])
    }

    func playObstacleAlert() {
        playTransients(at: [0, 0.12], sharpness: 0.7)
    }

    private func prepareEngine() {
        guard CHHapticEngine.capabilitiesForHardware().supportsHaptics else { return }
        do {
            engine = try CHHapticEngine()
            try engine?.start()
        } catch {
            engine = nil
        }
    }

    private func playTransients(at times: [TimeInterval], sharpness: Float = 0.55) {
        guard CHHapticEngine.capabilitiesForHardware().supportsHaptics else { return }
        let events = times.map { time in
            CHHapticEvent(
                eventType: .hapticTransient,
                parameters: [
                    CHHapticEventParameter(parameterID: .hapticIntensity, value: 1.0),
                    CHHapticEventParameter(parameterID: .hapticSharpness, value: sharpness)
                ],
                relativeTime: time
            )
        }
        play(events: events)
    }

    private func play(events: [CHHapticEvent]) {
        guard let engine else { return }
        do {
            if engine.isAutoShutdownEnabled {
                try engine.start()
            }
            let pattern = try CHHapticPattern(events: events, parameters: [])
            let player = try engine.makePlayer(with: pattern)
            try player.start(atTime: 0)
        } catch {
            try? engine.start()
        }
    }
}
