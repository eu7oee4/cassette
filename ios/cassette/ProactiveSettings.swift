import Foundation

/// 主动消息设置。字段与后端 /settings 的 JSON 一一对应（snake_case 由 CodingKeys 映射）。
/// 可选字段为 nil ＝ 关闭该限制（后端把缺省/null 当成"不限"）。
struct ProactiveSettings: Codable, Equatable {
    var agentName: String    // AI 的名字（首启引导设置，prompt/顶栏全用它）
    var userName: String     // 用户昵称（prompt 里怎么称呼用户）
    var enabled: Bool
    var activeStart: String        // "HH:MM"，活跃时段起
    var activeEnd: String          // "HH:MM"，活跃时段止（24:00＝次日零点）
    var dayFreq: String            // low | mid | high
    var nightFreq: String
    var dailyMax: Int?             // 每天最多几条；nil＝不限
    var minIntervalMin: Int?       // 两条主动消息最小间隔（分钟）；nil＝关闭
    var quietAfterUserMin: Int?    // 用户刚说过话后多久内不打扰（分钟）；nil＝关闭
    var wakeWindowN: Int?          // wake 注入窗口条数；nil＝后端默认 50（夹 20~300）
    var wakeDailyBudget: Int?      // 每天最多自发醒来次数；nil＝不限（拦醒来本身，省 token）
    var userPronoun: String        // 提到用户的人称代词：她 | 他 | TA

    enum CodingKeys: String, CodingKey {
        case enabled
        case agentName = "agent_name"
        case userName = "user_name"
        case activeStart = "active_start"
        case activeEnd = "active_end"
        case dayFreq = "day_freq"
        case nightFreq = "night_freq"
        case dailyMax = "daily_max"
        case minIntervalMin = "min_interval_min"
        case quietAfterUserMin = "quiet_after_user_min"
        case wakeWindowN = "wake_window_n"
        case wakeDailyBudget = "wake_daily_budget"
        case userPronoun = "user_pronoun"
    }

    init(agentName: String = "cassette", userName: String = "user",
         enabled: Bool = true,
         activeStart: String = "10:00", activeEnd: String = "24:00",
         dayFreq: String = "mid", nightFreq: String = "low",
         dailyMax: Int? = 10, minIntervalMin: Int? = 60, quietAfterUserMin: Int? = 20,
         wakeWindowN: Int? = 50, wakeDailyBudget: Int? = nil, userPronoun: String = "TA") {
        self.agentName = agentName
        self.userName = userName
        self.enabled = enabled
        self.activeStart = activeStart
        self.activeEnd = activeEnd
        self.dayFreq = dayFreq
        self.nightFreq = nightFreq
        self.dailyMax = dailyMax
        self.minIntervalMin = minIntervalMin
        self.quietAfterUserMin = quietAfterUserMin
        self.wakeWindowN = wakeWindowN
        self.wakeDailyBudget = wakeDailyBudget
        self.userPronoun = userPronoun
    }
}

/// 主动消息设置的本地持有者：本地即时读（UserDefaults），进页面与后端对齐，改动回写后端（后端才是执行者）。
@MainActor
final class ProactiveSettingsStore: ObservableObject {
    @Published var settings: ProactiveSettings

    private let key = "proactive_settings"
    private let service = ChatService()

    init() {
        if let data = UserDefaults.standard.data(forKey: key),
           let s = try? JSONDecoder().decode(ProactiveSettings.self, from: data) {
            settings = s
        } else {
            settings = ProactiveSettings()
        }
    }

    private func saveLocal() {
        if let d = try? JSONEncoder().encode(settings) {
            UserDefaults.standard.set(d, forKey: key)
        }
    }

    /// 进设置页时拉后端当前值对齐（后端是执行的真相）。连不上就保留本地值。
    func refreshFromServer() async {
        if let s = try? await service.getSettings() {
            settings = s
            saveLocal()
        }
    }

    /// 用户改了设置：先存本地，再同步给后端。
    func pushToServer() async {
        saveLocal()
        _ = try? await service.saveSettings(settings)
    }
}
