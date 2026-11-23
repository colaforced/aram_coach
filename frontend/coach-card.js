// 决策卡片与强化符文选择器（index.html 引用）。
// 只在三个时刻给明确答案：选人（换 / 骰 / 留）、阵亡或开局（买什么）、升级（选哪个强化符文）。
// 数据来自后端 state 的 verdict / rune_page / moment。
(function () {
'use strict';

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
// 第三方数据（OP.GG / CommunityDragon / 客户端）里的字符串都要转义后再拼进 innerHTML
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ESC[c]);
window.esc = esc;

const STYLE = `
.cc-card { background: #0f1923; border: 1px solid #1e2d3d; border-radius: 14px; padding: 16px 18px; margin-bottom: 14px; }
.cc-card.cc-swap { border-color: #64b5f6; box-shadow: 0 0 18px rgba(100,181,246,0.18); }
.cc-card.cc-reroll { border-color: #ffb74d; box-shadow: 0 0 18px rgba(255,183,77,0.18); }
.cc-card.cc-keep { border-color: #66bb6a; }
.cc-card.cc-shop { border-color: #ffd54f; box-shadow: 0 0 22px rgba(255,213,79,0.22); }
.cc-kicker { font-size: 12px; color: #78909c; margin-bottom: 4px; }
.cc-head { font-size: 24px; font-weight: 800; color: #eceff1; letter-spacing: .5px; }
.cc-reason { font-size: 13px; color: #b0bec5; margin-top: 6px; line-height: 1.5; }
.cc-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.cc-btn { background: #1e88e5; color: #fff; border: none; border-radius: 8px; padding: 8px 14px; font-size: 13px; font-weight: 600; cursor: pointer; }
.cc-btn:hover { filter: brightness(1.15); }
.cc-btn.secondary { background: #263646; color: #cfd8dc; }
.cc-btn:disabled { background: #263646; color: #607d8b; cursor: not-allowed; }
.cc-note { font-size: 11px; color: #607d8b; margin-top: 8px; }
.cc-options { margin-top: 12px; border-top: 1px solid #1e2d3d; padding-top: 10px; }
.cc-opt { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; color: #90a4ae; padding: 3px 0; }
.cc-opt.on { color: #eceff1; font-weight: 600; }
.cc-opt .cc-score { font-variant-numeric: tabular-nums; }
.cc-buys { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
.cc-buy { display: flex; align-items: baseline; gap: 10px; background: rgba(255,255,255,0.04); border-radius: 8px; padding: 8px 12px; }
.cc-buy .cc-name { font-size: 17px; font-weight: 700; color: #eceff1; flex: 1; }
.cc-buy.full .cc-name { color: #ffd54f; }
.cc-buy .cc-cost { font-size: 13px; color: #ffd54f; font-variant-numeric: tabular-nums; }
.cc-buy .cc-for { font-size: 12px; color: #78909c; }
.cc-sum { font-size: 12px; color: #90a4ae; margin-top: 8px; }
.cc-warn { font-size: 13px; color: #ffab91; margin-top: 8px; }
.cc-adj { font-size: 13px; color: #ef9a9a; margin-top: 6px; }
.cc-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.cc-chip { font-size: 12px; padding: 3px 9px; border-radius: 6px; background: rgba(255,255,255,0.05); color: #78909c; }
.cc-chip.done { color: #81c784; text-decoration: line-through; }
.cc-chip.next { color: #ffd54f; border: 1px solid #ffd54f; }
#cc-toast { position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%); background: #263646; color: #eceff1;
  padding: 10px 18px; border-radius: 10px; font-size: 13px; z-index: 10000; box-shadow: 0 6px 24px rgba(0,0,0,0.4); max-width: 80vw; }
#cc-toast.bad { background: #5d2626; }
#cc-aug-launch { position: fixed; right: 20px; bottom: 20px; z-index: 9000; background: linear-gradient(135deg,#7e57c2,#ec407a);
  color: #fff; border: none; border-radius: 24px; padding: 10px 18px; font-size: 14px; font-weight: 700; cursor: pointer;
  box-shadow: 0 6px 20px rgba(0,0,0,0.4); display: none; }
#cc-aug { position: fixed; right: 20px; top: 96px; width: 340px; max-width: calc(100vw - 40px); z-index: 9001;
  max-height: calc(100vh - 120px); overflow-y: auto;
  background: #121c2a; border: 1px solid #7e57c2; border-radius: 14px; padding: 14px 16px; box-shadow: 0 10px 36px rgba(0,0,0,0.55); }
#cc-aug h3 { font-size: 15px; color: #ce93d8; margin: 0 0 10px; display: flex; justify-content: space-between; }
#cc-aug h3 span { cursor: pointer; color: #78909c; }
#cc-aug input { width: 100%; box-sizing: border-box; background: #0a121c; border: 1px solid #2d4a5a; color: #eceff1;
  border-radius: 8px; padding: 8px 10px; font-size: 14px; margin-bottom: 6px; }
#cc-aug .cc-res { margin-top: 10px; font-size: 13px; color: #cfd8dc; }
#cc-aug .cc-ai { white-space: pre-wrap; line-height: 1.6; }
.cc-rank { padding: 6px 0; border-top: 1px solid #1e2d3d; }
.cc-rank.top .cc-rname { color: #ffd54f; font-weight: 800; font-size: 16px; }
.cc-rname { font-weight: 600; }
.cc-rwhy { font-size: 12px; color: #90a4ae; margin-top: 2px; }
.cc-rar { font-size: 10px; padding: 1px 5px; border-radius: 4px; margin-left: 6px; vertical-align: 2px; }
.cc-rar.silver { background: #607d8b; color: #fff; } .cc-rar.gold { background: #c9a227; color: #000; }
.cc-rar.prismatic { background: linear-gradient(90deg,#64b5f6,#ce93d8,#ffb74d); color: #000; }
`;
if (!document.getElementById('cc-style')) {
    const st = document.createElement('style');
    st.id = 'cc-style';
    st.textContent = STYLE;
    document.head.appendChild(st);
}

const pct = v => `${((v || 0) * 100).toFixed(1)}%`;

function toast(msg, ok = true) {
    let el = document.getElementById('cc-toast');
    if (!el) { el = document.createElement('div'); el.id = 'cc-toast'; document.body.appendChild(el); }
    el.className = ok ? '' : 'bad';
    el.textContent = msg;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.remove(), 4000);
}

async function post(url, body) {
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: body ? { 'Content-Type': 'application/json' } : {},
            body: body ? JSON.stringify(body) : undefined,
        });
        return await resp.json();
    } catch (e) {
        return { ok: false, reason: e.message };
    }
}

// ---------------------------------------------------------------- 选人：换 / 骰 / 留

function verdictCard(s) {
    const v = s.verdict;
    if (!v) return '';
    if (v.action === 'wait') return `<div class="cc-card"><div class="cc-head">🎲 ${esc(v.headline)}</div></div>`;
    const icon = { swap: '🔁', reroll: '🎲', keep: '✅' }[v.action] || '🎯';
    const buttons = [];
    if (v.action === 'swap' && v.target) {
        buttons.push(`<button class="cc-btn" onclick="CoachActions.swap(${Number(v.target.champion_id)})">换成 ${esc(v.target.name)}</button>`);
    }
    if (v.dice > 0) {
        buttons.push(`<button class="cc-btn ${v.action === 'reroll' ? '' : 'secondary'}" onclick="CoachActions.reroll()">🎲 重随（剩 ${Number(v.dice)}）</button>`);
    }
    const rp = s.rune_page || {};
    if (rp.available) {
        buttons.push(`<button class="cc-btn secondary" onclick="CoachActions.runes(${Number(s.my_champion_id)})">✨ 导入符文页（${rp.source === 'opgg' ? 'OP.GG' : '本地'}）</button>`);
    } else if (s.my_champion_id) {
        buttons.push(`<button class="cc-btn" disabled title="${esc(rp.reason)}">✨ 符文页不可用</button>`);
    }
    const rows = [v.current, ...(v.options || [])].filter(Boolean)
        .filter((o, i, arr) => arr.findIndex(x => x.champion_id === o.champion_id) === i)
        .map(o => {
            const on = v.target && o.champion_id === v.target.champion_id;
            const tag = o.champion_id === (v.current || {}).champion_id ? '（当前）' : '';
            const op = o.opgg && o.opgg.tier != null ? ` · T${esc(o.opgg.tier)} ${pct(o.opgg.win_rate)}` : '';
            return `<div class="cc-opt ${on ? 'on' : ''}"><span>${esc(o.name)}${tag} <small>${esc(o.role)}${op}</small></span>` +
                `<span class="cc-score">综合 ${o.score}（强度 ${o.strength} / 适配 ${o.fitness}）</span></div>`;
        }).join('');
    const kind = { swap: 'cc-swap', reroll: 'cc-reroll', keep: 'cc-keep' }[v.action] || '';
    return `<div class="cc-card ${kind}">
        <div class="cc-kicker">选人建议</div>
        <div class="cc-head">${icon} ${esc(v.headline)}</div>
        <div class="cc-reason">${esc(v.reason)}</div>
        <div class="cc-actions">${buttons.join('')}</div>
        ${rp.auto_apply ? '<div class="cc-note">已开启自动导入：换英雄后自动写入符文页</div>' : ''}
        ${rows ? `<div class="cc-options">${rows}</div>` : ''}
    </div>`;
}

// ---------------------------------------------------------------- 对局：买什么

function momentCard(s) {
    const m = s.moment;
    if (!m) return '';
    const p = m.plan || {};
    const title = {
        shop: `💀 复活前买（${Number(m.respawn)}s）`,
        start: '🛒 出门装',
        next: '🛒 下次阵亡时买',
    }[m.kind];
    const buys = (p.buys || []).map(b => `<div class="cc-buy ${b.full ? 'full' : ''}">
            <span class="cc-name">${esc(b.name)}</span>
            ${!b.full && b.for ? `<span class="cc-for">→ ${esc(b.for)}</span>` : ''}
            <span class="cc-cost">${Number(b.cost)}G</span></div>`).join('');
    const focus = p.focus ? `攒 <b>${esc(p.focus.name)}</b>：还差 ${Number(p.focus.missing)}G` : '';
    const sum = buys
        ? `现有 ${Number(p.gold)}G · 花 ${Number(p.spend)}G · 剩 ${Number(p.left)}G${focus ? ' · ' + focus : ''}`
        : `现有 ${Number(p.gold)}G，暂时买不起下一件的零件${focus ? '。' + focus : ''}`;
    const adj = (m.adjustments || []).map(a => `<div class="cc-adj">${a.kind === 'heal' ? '🩸' : '🛡️'} ${esc(a.tip)}</div>`).join('');
    const chips = (m.build_status || []).filter(b => b.owned)
        .map(b => `<span class="cc-chip done">${esc(b.name)}</span>`)
        .concat((m.targets || []).map((n, i) => `<span class="cc-chip ${i === 0 ? 'next' : ''}">${esc(n)}</span>`))
        .join('');
    return `<div class="cc-card ${m.kind === 'next' ? '' : 'cc-shop'}">
        <div class="cc-head" style="font-size:${m.kind === 'next' ? 18 : 22}px">${title}</div>
        ${buys ? `<div class="cc-buys">${buys}</div>` : ''}
        <div class="cc-sum">${sum}</div>
        ${p.inventory_full ? '<div class="cc-warn">⚠️ 装备栏满了：先卖掉不要的小件再买</div>' : ''}
        ${adj}
        ${chips ? `<div class="cc-chips">${chips}</div>` : ''}
    </div>`;
}

window.CoachCard = { verdictCard, momentCard, toast };

// ---------------------------------------------------------------- 按钮动作

window.CoachActions = {
    async swap(cid) {
        const r = await post(`/api/champ-select/swap/${cid}`);
        toast(r.ok ? '已交换英雄' : `交换失败：${r.reason || ''}`, r.ok);
    },
    async reroll() {
        const r = await post('/api/champ-select/reroll');
        toast(r.ok ? '已重随' : `重随失败：${r.reason || ''}`, r.ok);
    },
    async runes(cid) {
        const r = await post(`/api/runes/apply?champion_id=${cid}`);
        if (r.ok) toast(`符文页已${r.action === 'updated' ? '更新' : '创建'}：${r.champion}（${(r.perks || []).slice(0, 4).join(' / ')}）`);
        else toast(`符文页写入失败：${r.reason || ''}`, false);
    },
};

// ---------------------------------------------------------------- 强化符文三选一

const AugmentPicker = {
    pool: null,
    llm: false,
    async load() {
        if (this.pool) return;
        try {
            const r = await (await fetch('/api/augments')).json();
            this.pool = r.augments || [];
            this.llm = !!r.llm;
        } catch (e) {
            this.pool = [];
        }
    },
    showLauncher(visible) {
        // 页面里放了 [data-aug-launch] 按钮（比如顶栏）就用它，否则右下角悬浮一个
        let btn = document.querySelector('[data-aug-launch]') || document.getElementById('cc-aug-launch');
        if (!btn) {
            btn = document.createElement('button');
            btn.id = 'cc-aug-launch';
            btn.textContent = '🎲 强化符文三选一';
            btn.onclick = () => this.toggle();
            document.body.appendChild(btn);
        }
        btn.style.display = visible ? 'block' : 'none';
        if (!visible) this.close();
    },
    toggle() { document.getElementById('cc-aug') ? this.close() : this.open(); },
    close() { const el = document.getElementById('cc-aug'); if (el) el.remove(); },
    async open() {
        await this.load();
        if (document.getElementById('cc-aug')) { document.querySelector('#cc-aug input').focus(); return; }
        const el = document.createElement('div');
        el.id = 'cc-aug';
        const rarity = { silver: '银', gold: '金', prismatic: '彩' };
        el.innerHTML = `
            <h3>🎲 强化符文三选一 <span title="关闭">✕</span></h3>
            <datalist id="cc-aug-list">${this.pool.map(a =>
                `<option value="${esc(a.name)}">${esc(rarity[a.rarity] || '')}</option>`).join('')}</datalist>
            ${[1, 2, 3].map(i => `<input list="cc-aug-list" placeholder="选项 ${i}（输入几个字筛选）" autocomplete="off">`).join('')}
            <div class="cc-actions">
                <button class="cc-btn" data-act="rank">推荐</button>
                ${this.llm ? '<button class="cc-btn secondary" data-act="ai">问 AI</button>' : ''}
            </div>
            <div class="cc-res"></div>`;
        document.body.appendChild(el);
        el.querySelector('h3 span').onclick = () => this.close();
        el.querySelector('[data-act="rank"]').onclick = () => this.rank();
        const ai = el.querySelector('[data-act="ai"]');
        if (ai) ai.onclick = () => this.ai();
        el.querySelectorAll('input').forEach(inp => inp.addEventListener('keydown', e => { if (e.key === 'Enter') this.rank(); }));
        el.querySelector('input').focus();
    },
    options() {
        return [...document.querySelectorAll('#cc-aug input')].map(i => i.value.trim()).filter(Boolean);
    },
    async rank() {
        const opts = this.options();
        const res = document.querySelector('#cc-aug .cc-res');
        if (!res) return;
        if (!opts.length) { res.textContent = '先填入看到的选项'; return; }
        const r = await post('/api/augments/rank', { options: opts });
        const known = new Set((this.pool || []).map(a => a.name));
        const rows = (r.ranked || []).map((x, i) => `<div class="cc-rank ${i === 0 && r.pick ? 'top' : ''}">
                <span class="cc-rname">${i === 0 && r.pick ? '👉 ' : ''}${esc(x.name)}</span>
                ${x.rarity ? `<span class="cc-rar ${esc(x.rarity)}">${{ silver: '银', gold: '金', prismatic: '彩' }[x.rarity] || ''}</span>` : ''}
                ${known.has(x.name) ? '' : '<span class="cc-rar silver">不在符文池</span>'}
                <div class="cc-rwhy">${x.reasons && x.reasons.length ? esc(x.reasons.join('；')) : '知识库没有这个强化符文的数据'}</div>
            </div>`).join('');
        const head = r.pick
            ? (r.confident ? '' : '<div class="cc-rwhy">分差不大，按阵容和手感选也行</div>')
            : `<div class="cc-rwhy">知识库没覆盖这几个${this.llm ? '，可以点“问 AI”' : ''}</div>`;
        res.innerHTML = head + rows;
    },
    async ai() {
        const opts = this.options();
        const res = document.querySelector('#cc-aug .cc-res');
        if (!res || !opts.length) return;
        res.innerHTML = '<div class="cc-ai">🤖 思考中…</div>';
        const r = await post('/api/augments/ai', { options: opts });
        res.firstChild.textContent = (r.text || r.reason || '没有返回内容').replace(/\*\*/g, '');
    },
};
window.AugmentPicker = AugmentPicker;
})();
