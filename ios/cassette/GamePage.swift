import SwiftUI

// MARK: - DTO（照惯例：DTO 和 ChatService 扩展住在用它的页面文件顶部）

/// GET /game 的返回：引擎/急停/互斥/最近日志一把抓。
struct GameStatus: Decodable {
    let enabled: Bool
    let resource_ready: Bool
    let paused: Bool
    let lock_owner: String?
    let running: Bool
    let current: String?
    let queue: [String]
    let run_id: String?
    let recent: [GameLogEntry]?
}

/// task_log.jsonl 的一条（status: done/failed/error/skipped/summary）。
struct GameLogEntry: Decodable, Identifiable {
    let ts: Int
    let task: String
    let status: String
    let detail: String?
    let seconds: Int?
    let run_id: String?
    var id: String { "\(ts)-\(task)-\(status)" }
}

/// 任务的一个可选项（MaaYuan interface.json 的 option：下拉选一个 case）。
/// `default` 是 Swift 关键字，全仓唯一一处不得不写 CodingKeys。
struct GameTaskOption: Decodable {
    let name: String
    let cases: [String]
    let defaultCase: String?
    enum CodingKeys: String, CodingKey { case name, cases, defaultCase = "default" }
}

/// GET /game/tasks 的一项。separator=true 是 =====xxx===== 分组头，不可跑。
struct GameTaskItem: Decodable, Identifiable {
    let name: String
    let doc: String?
    let options: [GameTaskOption]?
    let separator: Bool?
    var id: String { name }
    var isSeparator: Bool { separator == true }
    /// 分组头的显示文字（剥掉两侧的 =）。
    var separatorTitle: String { name.trimmingCharacters(in: CharacterSet(charactersIn: "= ")) }
}

extension ChatService {
    func getGameStatus() async throws -> GameStatus {
        let data = try await perform(authedRequest("GET", "/game"))
        do { return try JSONDecoder().decode(GameStatus.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func setGamePaused(_ paused: Bool) async throws {
        struct Body: Encodable { let paused: Bool }
        let body = try JSONEncoder().encode(Body(paused: paused))
        _ = try await perform(authedRequest("POST", "/game", jsonBody: body))
    }

    func getGameTasks() async throws -> [GameTaskItem] {
        let data = try await perform(authedRequest("GET", "/game/tasks"))
        struct Wrap: Decodable { let tasks: [GameTaskItem] }
        do { return try JSONDecoder().decode(Wrap.self, from: data).tasks }
        catch { throw ChatServiceError.badResponse }
    }

    func startGameTasks(names: [String], options: [String: [String: String]]) async throws -> String? {
        struct Body: Encodable { let names: [String]; let options: [String: [String: String]] }
        struct Resp: Decodable { let ok: Bool?; let error: String? }
        let body = try JSONEncoder().encode(Body(names: names, options: options))
        // 起跑含设备自愈（冷启动模拟器最多 90s），超时放宽
        let data = try await perform(authedRequest("POST", "/game/tasks/start",
                                                   jsonBody: body, timeout: 120))
        let r = try? JSONDecoder().decode(Resp.self, from: data)
        return r?.error   // nil = 开跑了；有值 = 引擎给的有声拒绝（占用/急停中…）
    }

    func stopGameTasks() async throws {
        _ = try await perform(authedRequest("POST", "/game/tasks/stop"))
    }

    func getGameNotes(book: String) async throws -> String {
        struct Wrap: Decodable { let content: String }
        let data = try await perform(authedRequest("GET", "/game/notes/\(book)"))
        do { return try JSONDecoder().decode(Wrap.self, from: data).content }
        catch { throw ChatServiceError.badResponse }
    }

    func saveGameNotes(book: String, content: String) async throws {
        struct Body: Encodable { let content: String }
        let body = try JSONEncoder().encode(Body(content: content))
        _ = try await perform(authedRequest("POST", "/game/notes/\(book)", jsonBody: body))
    }
}

// MARK: - 游戏页

/// 抽屉 → 游戏：任务引擎的遥控台（勾任务/调选项/开跑/叫停/看收成）+ 两本笔记本。
/// 剧情会话不在这页管——那是聊天里 TA 自己切的，急停在顶栏 ⏸。
struct GamePage: View {
    private let service = ChatService()

    /// 任务列表区的固定高度：区内自己滚，「开跑」按钮贴在区下不用滑整页。
    private let taskAreaHeight: CGFloat = 340
    /// 选项选择的持久化（照 MaaYuan GUI 的思路：改过的下拉选择记住，下次还是它）。
    /// 只存本机——这是「从这页开跑」的偏好；TA 在聊天里派单有它自己的选项通道。
    @AppStorage("gameTaskOptionChoices") private var choicesJSON = "{}"

    @State private var status: GameStatus? = nil
    @State private var tasks: [GameTaskItem] = []
    @State private var selected: Set<String> = []
    @State private var expanded: Set<String> = []   // 展开选项面板的任务
    @State private var choices: [String: [String: String]] = [:]
    @State private var loading = true
    @State private var working = false
    @State private var errorText: String? = nil
    @State private var noteText: String? = nil
    @State private var noteSeq = 0

    var body: some View {
        Group {
            if loading {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let st = status, !st.enabled {
                ContentUnavailableView("Game 模式没开", systemImage: "gamecontroller",
                                       description: Text("在后端 server/.env 里设 GAME_MODE_ENABLED=1 并重启后端。"))
            } else if let st = status, !st.resource_ready {
                ContentUnavailableView("任务资源没就绪", systemImage: "shippingbox",
                                       description: Text("在 Mac 上跑一遍 server/tools/fetch_maayuan.py。"))
            } else if status == nil {
                ContentUnavailableView("读不到游戏状态", systemImage: "exclamationmark.triangle",
                                       description: Text(errorText ?? "连不上后端"))
            } else {
                content
            }
        }
        .navigationTitle("游戏")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task {
            loadChoices()
            await load()
        }
        .overlay(alignment: .bottom) {
            if let note = noteText {
                Text(note)
                    .font(.footnote).foregroundStyle(.white)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(Capsule().fill(Color.theme))
                    .padding(.bottom, 12)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: noteText)
        .alert("操作失败", isPresented: Binding(
            get: { errorText != nil && status != nil },
            set: { if !$0 { errorText = nil } }
        )) { Button("好", role: .cancel) { } } message: { Text(errorText ?? "") }
    }

    private var content: some View {
        List {
            engineSection
            taskSection
            notesSection      // 笔记本在「最近」上面：日志会越攒越长，别让本子沉底
            if let recent = status?.recent, !recent.isEmpty {
                recentSection(recent)
            }
        }
    }

    // MARK: 引擎状态

    private var engineSection: some View {
        Section("引擎") {
            if let st = status {
                if st.running {
                    HStack {
                        ProgressView().padding(.trailing, 6)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("正在跑：\(st.current ?? "…")")
                            if !st.queue.isEmpty {
                                Text("排队：\(st.queue.joined(separator: "、"))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        Button("叫停", role: .destructive) { stopRun() }
                            .disabled(working)
                    }
                } else {
                    Text(st.paused ? "⏸ 急停中（顶栏解除）" : "空闲")
                        .foregroundStyle(.secondary)
                }
                if let owner = st.lock_owner, owner == "story" {
                    Text("模拟器被剧情会话占着——TA 在玩，引擎暂时派不了单")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: 任务菜单（区内滚动，开跑按钮常驻区底）

    private var taskSection: some View {
        Section {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(tasks) { t in
                        if t.isSeparator {
                            separatorRow(t)
                        } else {
                            taskRow(t)
                            Divider().padding(.leading, 34)
                        }
                    }
                }
            }
            .frame(height: taskAreaHeight)
            .listRowInsets(EdgeInsets(top: 0, leading: 8, bottom: 0, trailing: 8))
            Button {
                startRun()
            } label: {
                HStack {
                    Spacer()
                    if working { ProgressView() }
                    else { Text("跑选中的（\(selected.count)）").bold() }
                    Spacer()
                }
            }
            .disabled(working || selected.isEmpty || status?.running == true)
        } header: {
            Text("任务（按菜单顺序执行，列表可上下滑）")
        } footer: {
            Text("点行选中，点 ⚙ 调该任务的选项（选择会记住）。游戏没开着时记得把「🚀 启动游戏」勾在最前面。")
        }
    }

    private func separatorRow(_ t: GameTaskItem) -> some View {
        HStack {
            Spacer()
            Text("— \(t.separatorTitle) —")
                .font(.caption).foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.vertical, 8)
        .background(Color(.systemGroupedBackground).opacity(0.6))
    }

    @ViewBuilder
    private func taskRow(_ t: GameTaskItem) -> some View {
        Button {
            if selected.contains(t.name) { selected.remove(t.name) }
            else { selected.insert(t.name) }
        } label: {
            HStack(spacing: 10) {
                Image(systemName: selected.contains(t.name)
                      ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(selected.contains(t.name) ? Color.theme : .secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text(t.name).foregroundStyle(.primary)
                    if let doc = t.doc, !doc.isEmpty {
                        Text(doc).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
                Spacer()
                if let opts = t.options, !opts.isEmpty {
                    Button {
                        if expanded.contains(t.name) { expanded.remove(t.name) }
                        else { expanded.insert(t.name) }
                    } label: {
                        Image(systemName: expanded.contains(t.name)
                              ? "gearshape.fill" : "gearshape")
                            .foregroundStyle(hasCustomChoices(t) ? Color.theme : .secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)   // 关键：不吃 List 的 tint，任务名保持正文黑
        if expanded.contains(t.name), let opts = t.options {
            optionPanel(task: t.name, options: opts)
        }
    }

    /// 选项面板：每个选项一个下拉（MaaYuan 自带 GUI 同款交互，cases 里选一个）。
    private func optionPanel(task: String, options: [GameTaskOption]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(options, id: \.name) { opt in
                Menu {
                    ForEach(opt.cases, id: \.self) { c in
                        Button {
                            setChoice(task: task, option: opt.name, value: c)
                        } label: {
                            if currentChoice(task: task, option: opt) == c {
                                Label(c, systemImage: "checkmark")
                            } else {
                                Text(c)
                            }
                        }
                    }
                } label: {
                    HStack {
                        Text(opt.name).font(.caption).foregroundStyle(.primary)
                        Spacer()
                        Text(currentChoice(task: task, option: opt) ?? "（默认）")
                            .font(.caption).foregroundStyle(Color.theme)
                        Image(systemName: "chevron.up.chevron.down")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 8)
                        .fill(Color(.systemGroupedBackground)))
                }
            }
        }
        .padding(.leading, 34)
        .padding(.bottom, 8)
    }

    // MARK: 最近收成

    private func recentSection(_ recent: [GameLogEntry]) -> some View {
        Section("最近") {
            ForEach(recent.reversed()) { e in
                HStack {
                    Text(statusMark(e.status))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(e.status == "summary" ? (e.detail ?? e.task) : e.task)
                            .font(.subheadline)
                        if e.status != "summary", let d = e.detail, !d.isEmpty {
                            Text(d).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                    Spacer()
                    Text(Date(timeIntervalSince1970: TimeInterval(e.ts)),
                         format: .dateTime.hour().minute())
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func statusMark(_ s: String) -> String {
        switch s {
        case "done":    return "✅"
        case "summary": return "🧾"
        case "skipped": return "⏸"
        default:        return "⚠️"
        }
    }

    // MARK: 笔记本

    private var notesSection: some View {
        Section("笔记本") {
            // 已 push 的页面里 value-based navigationDestination 不触发（DraftsPage 同款坑），
            // 直给目的地。
            NavigationLink { GameNotesEditor(book: "task", title: "任务本") } label: {
                Label("任务本", systemImage: "checklist")
            }
            NavigationLink { GameNotesEditor(book: "story", title: "剧情本") } label: {
                Label("剧情本", systemImage: "book")
            }
        }
    }

    // MARK: 选项持久化

    private func loadChoices() {
        if let data = choicesJSON.data(using: .utf8),
           let d = try? JSONDecoder().decode([String: [String: String]].self, from: data) {
            choices = d
        }
    }

    private func setChoice(task: String, option: String, value: String) {
        var t = choices[task] ?? [:]
        t[option] = value
        choices[task] = t
        if let data = try? JSONEncoder().encode(choices),
           let s = String(data: data, encoding: .utf8) {
            choicesJSON = s
        }
    }

    private func currentChoice(task: String, option: GameTaskOption) -> String? {
        choices[task]?[option.name] ?? option.defaultCase
    }

    private func hasCustomChoices(_ t: GameTaskItem) -> Bool {
        !(choices[t.name] ?? [:]).isEmpty
    }

    // MARK: 动作

    private func load() async {
        errorText = nil
        do {
            status = try await service.getGameStatus()
            if status?.enabled == true, status?.resource_ready == true {
                tasks = try await service.getGameTasks()
            }
        } catch {
            errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端"
        }
        loading = false
    }

    private func startRun() {
        working = true
        Task { @MainActor in
            defer { working = false }
            do {
                // 按菜单顺序跑，不按点选顺序——菜单顺序就是作者设计的日常顺序
                let names = tasks.filter { !$0.isSeparator }.map(\.name)
                    .filter { selected.contains($0) }
                let opts = choices.filter { names.contains($0.key) && !$0.value.isEmpty }
                if let err = try await service.startGameTasks(names: names, options: opts) {
                    errorText = err
                } else {
                    showNote("开跑了，结果会推送通知")
                    selected.removeAll()
                }
                await load()
            } catch {
                errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端"
            }
        }
    }

    private func stopRun() {
        working = true
        Task { @MainActor in
            defer { working = false }
            do {
                try await service.stopGameTasks()
                showNote("叫停了")
                await load()
            } catch {
                errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端"
            }
        }
    }

    private func showNote(_ text: String) {
        noteSeq += 1
        let seq = noteSeq
        noteText = text
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            if noteSeq == seq { noteText = nil }
        }
    }
}

// MARK: - 笔记本编辑器

/// 一本笔记的全文编辑（机主和 TA 共写；TA 那边是整本替换，这边也是）。
struct GameNotesEditor: View {
    let book: String     // task | story
    let title: String

    private let service = ChatService()
    @State private var text = ""
    @State private var loading = true
    @State private var saving = false
    @State private var dirty = false
    @State private var errorText: String? = nil

    var body: some View {
        Group {
            if loading {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                TextEditor(text: $text)
                    .font(.system(.footnote, design: .monospaced))
                    .padding(.horizontal, 8)
                    .onChange(of: text) { dirty = true }
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button {
                    save()
                } label: {
                    if saving { ProgressView() } else { Text("保存") }
                }
                .disabled(!dirty || saving)
            }
        }
        .task {
            do { text = try await service.getGameNotes(book: book) }
            catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端" }
            loading = false
            dirty = false
        }
        .alert("出错了", isPresented: Binding(
            get: { errorText != nil }, set: { if !$0 { errorText = nil } }
        )) { Button("好", role: .cancel) { } } message: { Text(errorText ?? "") }
    }

    private func save() {
        saving = true
        Task { @MainActor in
            defer { saving = false }
            do {
                try await service.saveGameNotes(book: book, content: text)
                dirty = false
            } catch {
                errorText = (error as? ChatServiceError)?.errorDescription ?? "保存失败"
            }
        }
    }
}
