import SwiftUI

/// 主动消息设置页。进页面拉后端对齐，任何改动即时回写后端。
struct ProactiveSettingsView: View {
    @ObservedObject var store: ProactiveSettingsStore
    @State private var loading = true
    @State private var pushTask: Task<Void, Never>? = nil   // 回写防抖（昵称每敲一字都触发 onChange）

    private let freqLabels: [(String, String)] = [("low", "低"), ("mid", "中"), ("high", "高")]

    var body: some View {
        Form {
                Section {
                    LabeledContent("TA 的昵称") {
                        TextField("cassette", text: field(\.agentName))
                            .multilineTextAlignment(.trailing)
                    }
                    LabeledContent("你的昵称") {
                        TextField("user", text: field(\.userName))
                            .multilineTextAlignment(.trailing)
                    }
                    Picker("提到你时用", selection: field(\.userPronoun)) {
                        Text("她").tag("她")
                        Text("他").tag("他")
                        Text("TA").tag("TA")
                    }
                    .pickerStyle(.segmented)
                } header: {
                    Text("昵称")
                } footer: {
                    Text("TA 的昵称也是顶栏标题。人称代词会用在聊天、内心独白和记忆里。")
                }

                Section {
                    Toggle("自主醒来", isOn: field(\.enabled))
                } footer: {
                    Text("开启后，TA 会自主醒来，自己决定要不要发消息给你。")
                }

                // wake 内部设置只在开关打开时显示（关=完全停摆，摆着一排没用的设置反而误导）。
                if store.settings.enabled {
                    Section("活跃时段") {
                        hourPicker("开始", \.activeStart, range: 0...23)
                        hourPicker("结束", \.activeEnd, range: 1...24)
                    }

                    Section {
                        freqPicker("活跃时段", \.dayFreq)
                        freqPicker("非活跃时段", \.nightFreq)
                    } header: {
                        Text("醒来频率")
                    } footer: {
                        Text("频率越高，TA 随机醒来的概率越大。")
                    }

                    Section {
                        optionalStepper("每天最多", \.dailyMax, defaultValue: 10, range: 1...30, step: 1, unit: "条")
                        optionalStepper("最小间隔", \.minIntervalMin, defaultValue: 60, range: 15...360, step: 15, unit: "分钟")
                        optionalStepper("刚聊过后静默", \.quietAfterUserMin, defaultValue: 20, range: 5...120, step: 5, unit: "分钟")
                    } header: {
                        Text("打扰控制")
                    } footer: {
                        Text("关掉开关=取消该限制\n前两项只拦消息，不拦 TA 醒来和思考。\n静默期间随机醒来被暂停，定时醒来不受影响。")
                    }
                }
        }
        .navigationTitle("设置")
        .navigationBarTitleDisplayMode(.inline)
        .disabled(loading)
        .overlay { if loading { ProgressView() } }
        .task {
            await store.refreshFromServer()
            loading = false
        }
        .onChange(of: store.settings) { _, _ in
            guard !loading else { return }   // 拉取对齐时的变化不回写
            // 防抖 600ms：打字类改动别每个字都 POST 一次。
            pushTask?.cancel()
            pushTask = Task {
                try? await Task.sleep(for: .milliseconds(600))
                guard !Task.isCancelled else { return }
                await store.pushToServer()
            }
        }
    }

    // MARK: - 行构造

    private func field<T>(_ kp: WritableKeyPath<ProactiveSettings, T>) -> Binding<T> {
        Binding(get: { store.settings[keyPath: kp] },
                set: { store.settings[keyPath: kp] = $0 })
    }

    /// 整点时间选择（"HH:00"）。结束支持 24:00。
    private func hourPicker(_ label: String, _ kp: WritableKeyPath<ProactiveSettings, String>,
                            range: ClosedRange<Int>) -> some View {
        let sel = Binding<Int>(
            get: { Int(store.settings[keyPath: kp].split(separator: ":").first ?? "0") ?? 0 },
            set: { store.settings[keyPath: kp] = String(format: "%02d:00", $0) }
        )
        return Picker(label, selection: sel) {
            ForEach(Array(range), id: \.self) { h in
                Text(String(format: "%02d:00", h)).tag(h)
            }
        }
    }

    private func freqPicker(_ label: String, _ kp: WritableKeyPath<ProactiveSettings, String>) -> some View {
        Picker(label, selection: field(kp)) {
            ForEach(freqLabels, id: \.0) { value, name in
                Text(name).tag(value)
            }
        }
    }

    /// 带开关的数值项：开关关＝该字段 nil（取消限制）；开＝显示 Stepper。
    private func optionalStepper(_ label: String, _ kp: WritableKeyPath<ProactiveSettings, Int?>,
                                 defaultValue: Int, range: ClosedRange<Int>, step: Int, unit: String) -> some View {
        let isOn = Binding<Bool>(
            get: { store.settings[keyPath: kp] != nil },
            set: { store.settings[keyPath: kp] = $0 ? defaultValue : nil }
        )
        return VStack(spacing: 8) {
            Toggle(label, isOn: isOn)
            if let val = store.settings[keyPath: kp] {
                Stepper("\(val) \(unit)", value: Binding(
                    get: { store.settings[keyPath: kp] ?? defaultValue },
                    set: { store.settings[keyPath: kp] = $0 }
                ), in: range, step: step)
            }
        }
    }
}
