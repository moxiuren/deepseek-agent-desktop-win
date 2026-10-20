using System.Text;

namespace DeepSeekAgent.Desktop;

/// <summary>
/// 增量输出的“封顶匀速”队列：只压 burst，平时零延迟。
///
/// 规则（和小莫拍定的三条一一对应）：
/// 1. 队列空时调用方**直接渲染**，本类碰都不碰 —— 快路径零惩罚；
/// 2. 有积压时新字进队列，由 UI 线程的 tick 按 <see cref="CharsPerTick"/>/40ms 匀速放出，
///    burst 再大也被摊平成肉眼顺滑，且自动追上（积压越大放得越久，不存在“切换”）；
/// 3. 轮末/工具开始/失败/停止时调用方负责 <see cref="Flush"/> 吐干净 ——
///    不会出现“最后一个字还在排队”的情况。
///
/// 本类是纯逻辑，不碰任何 WPF 类型（DispatcherTimer 由 AgentHost 持有）。
/// 以 1500 字/秒计：500 字的 burst 约 0.3 秒放完；60 字以下 burst 在 1 个 tick 内放完，
/// 和直吐体感无差 —— 这是“只压 burst”的数学含义。
/// 调手感只改 <see cref="CharsPerTick"/> 一个数。
/// </summary>
internal sealed class DeltaPacer
{
    /// <summary>每 tick 最多放出的字数。60 字/40ms ≈ 1500 字/秒。</summary>
    public const int CharsPerTick = 60;

    private readonly StringBuilder _buf = new();

    public int Pending => _buf.Length;

    public void Enqueue(string text)
    {
        if (!string.IsNullOrEmpty(text)) _buf.Append(text);
    }

    /// <summary>取出本 tick 该放出的字（最多 CharsPerTick），队列空则返回 ""。</summary>
    public string TakeTick()
    {
        if (_buf.Length == 0) return "";
        int n = System.Math.Min(CharsPerTick, _buf.Length);
        string chunk = _buf.ToString(0, n);
        _buf.Remove(0, n);
        return chunk;
    }

    /// <summary>全部吐出并清空，队列空则返回 ""。</summary>
    public string Flush()
    {
        if (_buf.Length == 0) return "";
        string all = _buf.ToString();
        _buf.Clear();
        return all;
    }

    /// <summary>丢弃全部（仅 ClearTranscript 用：对话流都清空了，排队的字已无处可去）。</summary>
    public void Clear() => _buf.Clear();
}
