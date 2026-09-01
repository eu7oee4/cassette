import SwiftUI

/// 问答卡（PLAN_chatui §3.5/§5，U4）：TA 用 AskUserQuestion 弹过来的选择题。
/// 染色走 §1.2 唯一口径（角色人物色 + 反色字）；一张卡里可能有几个问题，
/// 一次亮一个——选完一个，父层出小字提醒、这里自动浮现下一个（§5.2）；
/// 全答完父层 POST /questions/decide 一次性回填。
/// 二选一横排、更多竖列；可自定义答案；超时整卡置灰标「已超时」（§5.3）。
struct QuestionCardView: View {
    @Environment(\.colorScheme) private var scheme
    let card: QuestionCard
    let charID: String
    /// 每答完一个问题喊一次（父层出「眠眠选择了「…」」小字 §5.2）。
    var onPick: (_ question: String, _ answer: String) -> Void = { _, _ in }
    /// 所有问题都答完：父层拿完整 answers 去 POST decide。
    var onFinish: (_ answers: [String: String]) -> Void = { _ in }
    /// 先不答（整卡跳过，TA 收到一句「这会儿没答」）。
    var onSkip: () -> Void = { }
    /// 超时置灰后机主点「收起」。
    var onClose: () -> Void = { }

    @State private var idx = 0                      // 当前亮着的问题
    @State private var answers: [String: String] = [:]
    @State private var picked: Set<String> = []     // multiSelect 的勾选集
    @State private var custom = ""                  // 自定义答案输入
    @State private var expired = false
    @FocusState private var customFocused: Bool

    private var pal: CharPalette { IdentityColor.palette(for: charID) }
    private var fill: Color { expired ? Color(uiColor: .systemGray4) : ChatTint.fill(pal, scheme) }
    private var ink: Color { expired ? .secondary : ChatTint.text(scheme) }
    private var q: QuestionCard.Question? {
        idx < card.questions.count ? card.questions[idx] : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            headerRow
            if let q {
                Text(q.question)
                    .font(.subheadline.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                optionButtons(q)
                if !expired {
                    customRow(q)
                }
            }
        }
        .padding(14)
        .foregroundStyle(ink)
        .background(fill, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(alignment: .topLeading) {
            if expired {
                Text("已超时")
                    .font(.caption2.bold())
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Color(uiColor: .systemGray2), in: Capsule())
                    .foregroundStyle(.white)
                    .offset(x: 10, y: -8)
            }
        }
        .task(id: card.id) {
            guard let dl = card.deadline else { return }
            let wait = TimeInterval(dl) - Date().timeIntervalSince1970
            if wait > 0 {
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            }
            expired = true                          // 后端同刻已超时自拒，这儿只管置灰
        }
    }

    private var headerRow: some View {
        HStack {
            Text(q?.header?.isEmpty == false ? q!.header! : "想问你")
                .font(.caption2.bold())
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(ink.opacity(0.14), in: Capsule())
            if card.questions.count > 1 {
                Text("\(min(idx + 1, card.questions.count))/\(card.questions.count)")
                    .font(.caption2).opacity(0.7)
            }
            Spacer()
            if expired {
                Button("收起") { onClose() }
                    .font(.caption)
            } else {
                Button("先不答") { onSkip() }
                    .font(.caption).opacity(0.7)
            }
        }
        .buttonStyle(.plain)
    }

    /// 二选一横着排；两个以上竖着列（§3.5）。multiSelect 是勾选＋确定。
    @ViewBuilder private func optionButtons(_ q: QuestionCard.Question) -> some View {
        let multi = q.multiSelect == true
        let vertical = q.options.count != 2 || multi
        Group {
            if vertical {
                VStack(spacing: 6) {
                    ForEach(q.options, id: \.label) { opt in optionButton(opt, multi: multi) }
                }
            } else {
                HStack(spacing: 6) {
                    ForEach(q.options, id: \.label) { opt in optionButton(opt, multi: multi) }
                }
            }
        }
        if multi && !expired {
            Button {
                submit(picked.sorted().joined(separator: "、"))
            } label: {
                Text("就这些")
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                    .background(ink.opacity(picked.isEmpty ? 0.08 : 0.22),
                                in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            .buttonStyle(.plain)
            .disabled(picked.isEmpty)
        }
    }

    private func optionButton(_ opt: QuestionCard.Option, multi: Bool) -> some View {
        Button {
            guard !expired else { return }
            if multi {
                if picked.contains(opt.label) { picked.remove(opt.label) }
                else { picked.insert(opt.label) }
            } else {
                submit(opt.label)
            }
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    if multi {
                        Image(systemName: picked.contains(opt.label)
                              ? "checkmark.circle.fill" : "circle")
                            .font(.caption)
                    }
                    Text(opt.label).font(.subheadline.weight(.medium))
                }
                if let d = opt.description, !d.isEmpty {
                    Text(d).font(.caption2).opacity(0.75)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 10).padding(.vertical, 8)
            // 按钮圆角比容器小一号（§6 待定项，先按 12 走，机主看效果再改）
            .background(ink.opacity(picked.contains(opt.label) ? 0.26 : 0.14),
                        in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
    }

    /// 自定义答案（§5.2：功能同 Claude 原生，UI 自己做）。
    private func customRow(_ q: QuestionCard.Question) -> some View {
        HStack(spacing: 6) {
            TextField("自己写一个…", text: $custom)
                .font(.subheadline)
                .focused($customFocused)
                .padding(.horizontal, 10).padding(.vertical, 7)
                .background(ink.opacity(0.10),
                            in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .onSubmit { submitCustom() }
            Button { submitCustom() } label: {
                Image(systemName: "arrow.up.circle.fill").font(.title3)
            }
            .buttonStyle(.plain)
            .disabled(custom.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
    }

    private func submitCustom() {
        let t = custom.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        submit(t)
    }

    /// 答完当前问题：小字提醒 + 自动浮现下一张（§5.2）；最后一张收整卡。
    private func submit(_ answer: String) {
        guard let q else { return }
        answers[q.question] = answer
        onPick(q.question, answer)
        picked = []
        custom = ""
        customFocused = false
        if idx + 1 < card.questions.count {
            withAnimation(.easeInOut(duration: 0.18)) { idx += 1 }
        } else {
            onFinish(answers)
        }
    }
}
