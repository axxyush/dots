import AVFoundation
import Foundation
import Speech
import SwiftUI

/// Manual speech command listener used by the live navigation mic button.
///
/// The type keeps the old name so existing call sites stay stable, but it
/// no longer starts any passive wake-word recognition while live AR is active.
@MainActor
final class WakeWordListener: ObservableObject {
    @Published private(set) var isCommandActive = false
    @Published private(set) var commandText: String = ""
    @Published private(set) var errorMessage: String?

    /// Called when manual listening starts.
    var onWakeWord: (() -> Void)?
    /// Called when a command is ready (after silence timeout).
    var onCommandReady: ((String) -> Void)?

    private let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private let audioEngine = AVAudioEngine()

    private var silenceTimer: Timer?
    private let initialSilenceThreshold: TimeInterval = 6.0
    private let silenceThreshold: TimeInterval = 3.0
    private var lastSpeechDate = Date.distantPast
    private var commandSessionStartDate = Date.distantPast
    private var commandBuffer = ""
    private var hasInputTap = false
    private var hasRecognizedSpeech = false
    private var earlyRestartAttempts = 0
    private var recognitionToken = UUID()

    func stopEverything(clearErrorMessage: Bool = true) {
        silenceTimer?.invalidate()
        silenceTimer = nil

        teardownRecognitionPipeline()
        isCommandActive = false
        commandBuffer = ""
        commandText = ""
        if clearErrorMessage {
            errorMessage = nil
        }
    }

    func activateManually() async -> Bool {
        guard !isCommandActive else { return true }
        errorMessage = nil

        guard await requestPermissionsIfNeeded() else {
            return false
        }

        guard let speechRecognizer, speechRecognizer.isAvailable else {
            errorMessage = "Speech recognition is not available right now."
            print("[WakeWord] Speech recognizer not available.")
            return false
        }

        isCommandActive = true
        commandBuffer = ""
        commandText = ""
        lastSpeechDate = .distantPast
        commandSessionStartDate = Date()
        hasRecognizedSpeech = false
        earlyRestartAttempts = 0
        onWakeWord?()
        startCommandListening()
        return isCommandActive
    }

    func finishCommandCapture() {
        guard isCommandActive else { return }
        submitCommandIfReady()
    }

    private func startCommandListening() {
        guard let speechRecognizer, speechRecognizer.isAvailable else {
            errorMessage = "Speech recognition is not available right now."
            stopEverything(clearErrorMessage: false)
            return
        }

        do {
            teardownRecognitionPipeline()

            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(
                .playAndRecord,
                mode: .default,
                options: [.mixWithOthers, .defaultToSpeaker, .allowBluetoothHFP]
            )
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)

            recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
            guard let recognitionRequest else { return }

            if #available(iOS 13.0, *) {
                recognitionRequest.requiresOnDeviceRecognition = speechRecognizer.supportsOnDeviceRecognition
            }
            recognitionRequest.shouldReportPartialResults = true
            let token = UUID()
            recognitionToken = token

            recognitionTask = speechRecognizer.recognitionTask(with: recognitionRequest) { [weak self] result, error in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    guard self.isCommandActive, self.recognitionToken == token else { return }

                    if let result {
                        let transcript = result.bestTranscription.formattedString.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !transcript.isEmpty {
                            self.hasRecognizedSpeech = true
                            self.commandBuffer = transcript
                            self.commandText = transcript
                            self.lastSpeechDate = Date()
                            self.startSilenceTimer()
                        }
                    }

                    if error != nil || (result?.isFinal == true) {
                        self.handleTerminalRecognitionEvent(error: error, isFinal: result?.isFinal == true)
                    }
                }
            }

            let inputNode = audioEngine.inputNode
            let recordingFormat = inputNode.outputFormat(forBus: 0)
            guard recordingFormat.channelCount > 0, recordingFormat.sampleRate > 0 else {
                errorMessage = "Microphone input is not ready yet. Please try again."
                print("[WakeWord] Cannot start command listening - input format is unavailable.")
                stopEverything(clearErrorMessage: false)
                return
            }

            inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak self] buffer, _ in
                guard buffer.frameLength > 0 else { return }
                self?.recognitionRequest?.append(buffer)
            }
            hasInputTap = true

            audioEngine.prepare()
            try audioEngine.start()
            startSilenceTimer()
            print("[WakeWord] Command listening started (format: \(recordingFormat)).")
        } catch {
            errorMessage = "Could not start the microphone: \(error.localizedDescription)"
            print("[WakeWord] Command listen failed: \(error.localizedDescription)")
            stopEverything(clearErrorMessage: false)
        }
    }

    private func startSilenceTimer() {
        silenceTimer?.invalidate()
        silenceTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                let now = Date()
                if !self.hasRecognizedSpeech {
                    if now.timeIntervalSince(self.commandSessionStartDate) >= self.initialSilenceThreshold {
                        self.errorMessage = "I didn't catch that. Hold the phone steady and try the mic again."
                        self.stopEverything(clearErrorMessage: false)
                    }
                    return
                }

                if now.timeIntervalSince(self.lastSpeechDate) >= self.silenceThreshold {
                    self.submitCommandIfReady()
                }
            }
        }
    }

    private func submitCommandIfReady() {
        guard isCommandActive else { return }
        silenceTimer?.invalidate()
        silenceTimer = nil

        let finalText = commandBuffer.trimmingCharacters(in: .whitespacesAndNewlines)
        print("[WakeWord] Command submitted: \(finalText)")

        teardownRecognitionPipeline()
        isCommandActive = false
        commandBuffer = ""
        commandText = ""

        if finalText.isEmpty {
            errorMessage = hasRecognizedSpeech
                ? "Speech recognition stopped before the full command came through. Try again."
                : "I didn't catch that. Hold the phone steady and try the mic again."
            return
        }

        onCommandReady?(finalText)
    }

    private func handleTerminalRecognitionEvent(error: Error?, isFinal: Bool) {
        let finalText = commandBuffer.trimmingCharacters(in: .whitespacesAndNewlines)
        if !finalText.isEmpty {
            submitCommandIfReady()
            return
        }

        let elapsed = Date().timeIntervalSince(commandSessionStartDate)
        let endedTooQuickly = elapsed < 1.0

        if !hasRecognizedSpeech, endedTooQuickly, earlyRestartAttempts < 2 {
            earlyRestartAttempts += 1
            restartCommandListeningAfterEarlyTermination()
            return
        }

        if !hasRecognizedSpeech {
            errorMessage = "I didn't catch that. Hold the phone steady and try the mic again."
        } else if let error {
            errorMessage = "Speech recognition stopped early: \(error.localizedDescription)"
        } else if isFinal {
            errorMessage = "Speech recognition stopped before the full command came through. Try again."
        }

        stopEverything(clearErrorMessage: false)
    }

    private func restartCommandListeningAfterEarlyTermination() {
        silenceTimer?.invalidate()
        silenceTimer = nil
        teardownRecognitionPipeline()

        Task { @MainActor [weak self] in
            guard let self else { return }
            try? await Task.sleep(nanoseconds: 200_000_000)
            guard self.isCommandActive else { return }
            self.startCommandListening()
        }
    }

    private func requestPermissionsIfNeeded() async -> Bool {
        let speechAuthorized = await requestSpeechRecognitionPermissionIfNeeded()
        guard speechAuthorized else {
            errorMessage = "Enable speech recognition in Settings to use the mic."
            return false
        }

        let microphoneAuthorized = await requestMicrophonePermissionIfNeeded()
        guard microphoneAuthorized else {
            errorMessage = "Enable microphone access in Settings to use the mic."
            return false
        }

        return true
    }

    private func requestSpeechRecognitionPermissionIfNeeded() async -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized:
            return true
        case .notDetermined:
            return await withCheckedContinuation { continuation in
                SFSpeechRecognizer.requestAuthorization { status in
                    continuation.resume(returning: status == .authorized)
                }
            }
        case .denied, .restricted:
            return false
        @unknown default:
            return false
        }
    }

    private func requestMicrophonePermissionIfNeeded() async -> Bool {
        if #available(iOS 17.0, *) {
            return await AVAudioApplication.requestRecordPermission()
        }

        return await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    private func teardownRecognitionPipeline() {
        recognitionToken = UUID()
        if audioEngine.isRunning {
            audioEngine.stop()
        }
        if hasInputTap {
            audioEngine.inputNode.removeTap(onBus: 0)
            hasInputTap = false
        }
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        recognitionTask?.cancel()
        recognitionTask = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}
