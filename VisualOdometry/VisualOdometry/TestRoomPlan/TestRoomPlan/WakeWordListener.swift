import AVFoundation
import Foundation
import Speech
import SwiftUI

/// Always-on keyword listener for "Hey Dots".
///
/// Uses Apple's `SFSpeechRecognizer` in streaming mode to continuously
/// monitor microphone input. When the wake phrase is detected, it fires
/// the `onWakeWord` callback and enters "active command" mode where it
/// records the user's request and auto-submits after a silence timeout.
@MainActor
final class WakeWordListener: ObservableObject {
    @Published private(set) var isPassiveListening = false
    @Published private(set) var isCommandActive = false
    @Published private(set) var commandText: String = ""

    /// Called when the wake word is detected.
    var onWakeWord: (() -> Void)?
    /// Called when a command is ready (after silence timeout).
    var onCommandReady: ((String) -> Void)?

    private let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private let audioEngine = AVAudioEngine()

    private var silenceTimer: Timer?
    private let silenceThreshold: TimeInterval = 2.0
    private var lastSpeechDate = Date()
    private var wakeWordDetected = false
    private var commandBuffer = ""

    // MARK: - Public API

    /// Start passive "Hey Dots" listening.
    func startPassiveListening() {
        guard !isPassiveListening else { return }
        stopEverything()

        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playAndRecord, mode: .default, options: [.duckOthers, .defaultToSpeaker, .allowBluetooth])
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)

            recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
            guard let recognitionRequest else { return }

            if #available(iOS 13.0, *) {
                recognitionRequest.requiresOnDeviceRecognition = speechRecognizer?.supportsOnDeviceRecognition ?? false
            }
            recognitionRequest.shouldReportPartialResults = true

            recognitionTask = speechRecognizer?.recognitionTask(with: recognitionRequest) { [weak self] result, error in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    if let result {
                        self.handleRecognitionResult(result)
                    }
                    if error != nil || (result?.isFinal == true) {
                        // Restart passive listening if it stopped unexpectedly
                        if self.isPassiveListening && !self.isCommandActive {
                            self.restartPassiveListening()
                        }
                    }
                }
            }

            let inputNode = audioEngine.inputNode
            let recordingFormat = inputNode.outputFormat(forBus: 0)
            inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak self] buffer, _ in
                self?.recognitionRequest?.append(buffer)
            }

            audioEngine.prepare()
            try audioEngine.start()
            isPassiveListening = true
            print("[WakeWord] Passive listening started.")
        } catch {
            print("[WakeWord] Failed to start: \(error.localizedDescription)")
        }
    }

    /// Stop all listening (passive + active).
    func stopEverything() {
        silenceTimer?.invalidate()
        silenceTimer = nil

        if audioEngine.isRunning {
            audioEngine.stop()
            audioEngine.inputNode.removeTap(onBus: 0)
        }
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        recognitionTask?.cancel()
        recognitionTask = nil

        isPassiveListening = false
        isCommandActive = false
        wakeWordDetected = false
        commandBuffer = ""
        commandText = ""
    }

    /// Temporarily pause listening (e.g., while TTS is speaking).
    func pauseListening() {
        silenceTimer?.invalidate()
        if audioEngine.isRunning {
            audioEngine.pause()
        }
    }

    /// Resume after pause.
    func resumeListening() {
        if !audioEngine.isRunning {
            try? audioEngine.start()
        }
        if isCommandActive {
            startSilenceTimer()
        }
    }

    // MARK: - Recognition Handling

    private func handleRecognitionResult(_ result: SFSpeechRecognitionResult) {
        let text = result.bestTranscription.formattedString.lowercased()

        if !wakeWordDetected {
            // Check for wake word
            if text.contains("hey dots") || text.contains("hey dot") || text.contains("hey das") {
                wakeWordDetected = true
                isCommandActive = true
                commandBuffer = ""
                lastSpeechDate = Date()
                onWakeWord?()
                print("[WakeWord] Wake word detected!")

                // Stop current recognition and restart for command capture
                restartForCommand()
                return
            }
        } else {
            // In command mode: capture everything after wake word
            commandBuffer = result.bestTranscription.formattedString
            commandText = commandBuffer
            lastSpeechDate = Date()

            // Reset silence timer on every speech update
            startSilenceTimer()
        }
    }

    private func restartForCommand() {
        // Stop current session
        if audioEngine.isRunning {
            audioEngine.stop()
            audioEngine.inputNode.removeTap(onBus: 0)
        }
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        recognitionTask?.cancel()
        recognitionTask = nil

        // Start fresh recognition for the command
        do {
            recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
            guard let recognitionRequest else { return }

            if #available(iOS 13.0, *) {
                recognitionRequest.requiresOnDeviceRecognition = speechRecognizer?.supportsOnDeviceRecognition ?? false
            }
            recognitionRequest.shouldReportPartialResults = true

            recognitionTask = speechRecognizer?.recognitionTask(with: recognitionRequest) { [weak self] result, error in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    if let result {
                        self.commandBuffer = result.bestTranscription.formattedString
                        self.commandText = self.commandBuffer
                        self.lastSpeechDate = Date()
                        self.startSilenceTimer()
                    }
                    if error != nil || (result?.isFinal == true) {
                        self.submitCommandIfReady()
                    }
                }
            }

            let inputNode = audioEngine.inputNode
            let recordingFormat = inputNode.outputFormat(forBus: 0)
            inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak self] buffer, _ in
                self?.recognitionRequest?.append(buffer)
            }

            audioEngine.prepare()
            try audioEngine.start()

            // Start silence timer
            startSilenceTimer()
        } catch {
            print("[WakeWord] Failed to restart for command: \(error.localizedDescription)")
            submitCommandIfReady()
        }
    }

    private func startSilenceTimer() {
        silenceTimer?.invalidate()
        silenceTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if Date().timeIntervalSince(self.lastSpeechDate) >= self.silenceThreshold {
                    self.submitCommandIfReady()
                }
            }
        }
    }

    private func submitCommandIfReady() {
        silenceTimer?.invalidate()
        silenceTimer = nil

        let finalText = commandBuffer.trimmingCharacters(in: .whitespacesAndNewlines)
        print("[WakeWord] Command submitted: \(finalText)")

        // Clean up command state
        if audioEngine.isRunning {
            audioEngine.stop()
            audioEngine.inputNode.removeTap(onBus: 0)
        }
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        recognitionTask?.cancel()
        recognitionTask = nil

        isCommandActive = false
        wakeWordDetected = false

        if !finalText.isEmpty {
            onCommandReady?(finalText)
        }

        // Restart passive listening after a brief delay (let TTS play first)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startPassiveListening()
        }
    }

    private func restartPassiveListening() {
        stopEverything()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.startPassiveListening()
        }
    }
}
