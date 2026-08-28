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
    var randomWake: Bool?          // 随机概率醒来；nil＝开（旧缓存没这键）。只关随机不动定时/事件

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
        case randomWake = "random_wake"
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
///
/// **手里这份设置属于谁（charID）跟着值一起走**，不认全局 CurrentCharacter——设置页
/// 右上角就挂着切人按钮，「拉取在飞」和「回写防抖中」这两个窗口里全局都可能已经变了。
/// 2026-08-28 事故就是这么来的：default 的整份设置被 POST 进 cass，agent_name 把
/// char.json 的 Cassius 覆盖成了 cassette，两个角色显示同名。所以：
///   - 拉取带上请求时的角色，回来发现人已经换了就丢弃（不是当前这份，别覆盖）；
///   - 回写把 (值, 角色) 成对快照，防抖期间切人照样写回它原本属于的那个角色；
///   - 装载引起的 settings 变化不算「用户改了设置」，不触发回写。
@MainActor
final class ProactiveSettingsStore: ObservableObject {
    @Published var settings: ProactiveSettings
    /// settings 属于哪个角色（存盘/回写都认它）。
    private(set) var charID: String
    /// 正在装载（切人/拉后端对齐）：这期间 settings 的变化不是用户改的。
    @Published private(set) var loading = false

    private let service = ChatService()
    private var pushTask: Task<Void, Never>? = nil
    /// 已排期、还没落地的回写快照（角色 + 值）。
    private var pending: (char: String, value: ProactiveSettings)? = nil

    // 本地缓存按角色分 key（默认角色沿用老 key，零迁移）。
    private static func key(_ id: String) -> String {
        id == "default" ? "proactive_settings" : "proactive_settings_\(id)"
    }

    init() {
        let id = CurrentCharacter.id
        charID = id
        settings = Self.loadLocal(key: Self.key(id))
    }

    private static func loadLocal(key: String) -> ProactiveSettings {
        if let data = UserDefaults.standard.data(forKey: key),
           let s = try? JSONDecoder().decode(ProactiveSettings.self, from: data) {
            return s
        }
        // 没缓存时**不要**替这个角色编一个名字：空＝后端按 char.json / 兜底默认取名。
        // 给了 "cassette" 的话，缓存缺席的角色一旦被回写就会被按上默认角色的名字。
        return ProactiveSettings(agentName: "")
    }

    /// 切会话后：先上本地缓存的该角色设置（即时），再找后端对齐。
    func reloadForCurrentCharacter() async {
        flushPending()          // 上一位还没落地的改动先送走（带它自己的角色）
        let id = CurrentCharacter.id
        loading = true
        charID = id
        settings = Self.loadLocal(key: Self.key(id))
        await refreshFromServer()
        loading = false
    }

    private func saveLocal(_ value: ProactiveSettings, char: String) {
        if let d = try? JSONEncoder().encode(value) {
            UserDefaults.standard.set(d, forKey: Self.key(char))
        }
    }

    /// 进页面拉后端当前值对齐（后端是执行的真相）。连不上就保留本地值。
    /// 在飞期间切了人 → 这份是上一位的，丢掉。
    func refreshFromServer() async {
        // 本地还有没送到后端的改动 → 先送走再读，否则这一读会拿后端的旧值把它盖回去
        // （用户看到的是「我改的设置自己变回去了」）。必须 await：fire-and-forget 的话
        // GET 可能先于 POST 落地，就还是拿旧值覆盖。
        await flushPendingNow()
        let id = charID
        let outer = loading
        loading = true
        defer { loading = outer }
        if let s = try? await service.getSettings(char: id), charID == id {
            settings = s
            saveLocal(s, char: id)
        }
    }

    /// 用户改了设置：600ms 防抖后回写（打字类改动别每个字一发）。
    /// 快照连角色一起捕获——防抖期间切人，这一发照样落在它本来那个角色头上。
    func schedulePush(debounceMs: Int = 600) {
        guard !loading else { return }   // 装载引起的变化不是用户改的
        if let p = pending, p.char != charID { flushPending() }
        let snapshot = settings, id = charID
        pending = (id, snapshot)
        saveLocal(snapshot, char: id)
        pushTask?.cancel()
        pushTask = Task { [service] in
            try? await Task.sleep(for: .milliseconds(debounceMs))
            guard !Task.isCancelled else { return }
            do {
                _ = try await service.saveSettings(snapshot, char: id)
                if self.pending?.char == id { self.pending = nil }
            } catch {
                // 没送到就**留着 pending**（本地已经是新值了）：清掉的话后端还是旧值，
                // 下次进页面对齐就把它静默盖回去。留着 → 下次 refresh 前先补送。
            }
        }
    }

    /// 立即回写（不防抖）：给「输入框提交 / 改昵称 / 引导收尾」这类一次性动作用。
    func pushToServer() async {
        pushTask?.cancel()
        let snapshot = settings, id = charID
        pending = nil
        saveLocal(snapshot, char: id)
        _ = try? await service.saveSettings(snapshot, char: id)
    }

    /// 把排期中的回写立刻送出去（切人前用）：不等防抖，也不管现在在看谁。
    /// 送的是**上一位**的值和角色，和随后要读的新角色不冲突，所以不用等它回来。
    private func flushPending() {
        pushTask?.cancel()
        guard let p = pending else { return }
        pending = nil
        Task { [service] in _ = try? await service.saveSettings(p.value, char: p.char) }
    }

    /// 同上，但**等它送完**：给「马上要读同一个角色」的调用方用（refreshFromServer）。
    /// 失败就把 pending 放回去，下次再补送——绝不能一边丢掉改动一边去拉旧值。
    private func flushPendingNow() async {
        pushTask?.cancel()
        guard let p = pending else { return }
        pending = nil
        do { _ = try await service.saveSettings(p.value, char: p.char) }
        catch { pending = p }
    }
}
