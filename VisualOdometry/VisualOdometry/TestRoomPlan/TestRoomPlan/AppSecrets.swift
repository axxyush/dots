import Foundation

/// Central secrets for API keys. Replace placeholder values with real keys.
enum AppSecrets {
    private static let dotenvValues: [String: String] = loadDotenv()

    /// ZETIC Melange personal key for on-device AI model downloads.
    static let zeticPersonalKey: String = {
        value(for: "ZETIC_PERSONAL_KEY")
    }()

    /// Gemini API key for room-aware Q&A in the navigation assistant.
    static let geminiApiKey: String = {
        value(for: "GEMINI_API_KEY")
    }()

    /// Gemini model used for room-aware Q&A.
    static let geminiModel: String = {
        let resolved = value(for: "GEMINI_MODEL")
        if !resolved.isEmpty {
            return resolved
        }
        return "gemini-2.5-flash"
    }()

    /// ElevenLabs API key for voice agent TTS. Set via Info.plist or hardcode below.
    static let elevenLabsApiKey: String = {
        value(for: "ELEVENLABS_API_KEY")
    }()

    /// ElevenLabs voice ID used for navigation TTS.
    static let elevenLabsVoiceId: String = {
        value(for: "ELEVENLABS_VOICE_ID")
    }()

    /// ElevenLabs agent ID for the conversational voice agent.
    static let elevenLabsAgentId: String = {
        value(for: "ELEVENLABS_AGENT_ID")
    }()

    private static func value(for key: String) -> String {
        if let plistValue = Bundle.main.infoDictionary?[key] as? String {
            let trimmed = plistValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                return trimmed
            }
        }

        if let environmentValue = ProcessInfo.processInfo.environment[key] {
            let trimmed = environmentValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                return trimmed
            }
        }

        return dotenvValues[key] ?? ""
    }

    private static func loadDotenv() -> [String: String] {
        let candidateURLs = [
            Bundle.main.resourceURL?.appendingPathComponent(".env"),
            Bundle.main.bundleURL.appendingPathComponent(".env"),
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(".env")
        ]

        for url in candidateURLs.compactMap({ $0 }) {
            guard let contents = try? String(contentsOf: url, encoding: .utf8) else {
                continue
            }

            var values: [String: String] = [:]
            for rawLine in contents.components(separatedBy: .newlines) {
                let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
                if line.isEmpty || line.hasPrefix("#") {
                    continue
                }

                let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
                guard parts.count == 2 else {
                    continue
                }

                let key = String(parts[0]).trimmingCharacters(in: .whitespacesAndNewlines)
                let value = String(parts[1]).trimmingCharacters(in: .whitespacesAndNewlines)
                values[key] = value
            }
            return values
        }

        return [:]
    }
}
