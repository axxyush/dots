import Foundation

/// Central secrets for API keys. Replace placeholder values with real keys.
enum AppSecrets {
    /// ZETIC Melange personal key for on-device AI model downloads.
    static let zeticPersonalKey: String = {
        if let key = Bundle.main.infoDictionary?["ZETIC_PERSONAL_KEY"] as? String, !key.isEmpty {
            return key
        }
        // Fallback: hardcode your key here during development
        return "dev_68a8fcf14f3e413ca1a88192f44446aa"
    }()
}
