import{withdrawals}from'../stores/withdrawal.js';
import{eligibleWithdrawals,createWithdrawals,retryWithdrawal}from'../api/withdrawal.js';
import{batches}from'../stores/batch.js';
import{header}from'../components/common/header.js';
import{table}from'../components/common/table.js';
import{esc}from'../utils/html.js';
import{notify}from'../hooks/feedback.js';
import{session}from'../stores/session.js';
import{WITHDRAW_STATUSES}from'../constants/enums.js';

const FILTERS=[['','全部'],['pending','待处理'],['effective','已生效'],['failed','失败']];
const wbadge=s=>`<span class="badge ${esc(s)}">${esc(WITHDRAW_STATUSES[s]||s)}</span>`;
const canWrite=()=>session.get().role!=='viewer';

export async function withdrawalsPage(el){
  const filter=(location.hash.split('/')[1]||'').split('?')[0];
  const allBatches=await batches.load();
  const doneBatches=allBatches.filter(b=>['passed','different','failed'].includes(b.status));
  const sel=Number(new URLSearchParams(location.hash.split('?')[1]||'').get('batch'))||doneBatches[0]?.id;
  const records=await withdrawals.load(filter?('?status='+filter):'');
  let eligible=null;
  if(sel){try{eligible=await eligibleWithdrawals(sel)}catch(e){eligible={error:e.message}}}
  const batchName=id=>{const b=allBatches.find(x=>x.id===id);return b?('#'+id+' · '+b.name):('#'+id)};
  el.innerHTML=header('05','到期账单撤下登记','只对两侧都带账单号、且已到期的账单登记撤下。生效后旧档与并发复核的页面、下载和差异样本都不再带回到期行；原结论、行数与其余账单照旧可查。')
    +`<div class="panel"><div class="actions" style="margin-top:0">
        <label style="margin:0">筛选状态</label>
        ${FILTERS.map(([k,v])=>`<a class="badge ${filter===k?'active':''}" href="#withdrawals${k?('/'+k):''}">${esc(v)}</a>`).join('')}
        <span class="muted" style="margin-left:auto">${records.length} 条登记</span>
      </div>
      ${table(['登记 / 批次','账单号','状态','失败原因','更新时间','操作'],records.map(x=>[
        '<span class="mono">#'+x.id+'</span><br>'+esc(batchName(x.batch_id)),
        '<span class="mono">'+esc(x.invoice_id)+'</span>',
        wbadge(x.status),
        x.fail_reason?('<span class="error">'+esc(x.fail_reason)+'</span>'):'—',
        esc(x.updated_at),
        x.status==='failed'&&canWrite()?`<button data-retry="${x.id}">重试</button>`:(x.status==='effective'?'已完成':'处理中')
      ]))}
    </div>`
    +(sel?renderRegister(sel,eligible,canWrite()):'<div class="empty">还没有已完成的复核批次。先到 SQL 复核页用“逐单结算明细”样本跑一个批次。</div>');

  el.querySelectorAll('[data-retry]').forEach(btn=>{
    btn.onclick=async()=>{try{await retryWithdrawal(Number(btn.dataset.retry));notify('已重新排队，沿用原登记不新增');await withdrawalsPage(el)}catch(e){notify(e.message)}};
  });
  const form=el.querySelector('#register-form');
  if(form)form.onsubmit=async e=>{
    e.preventDefault();
    const ids=[...form.querySelectorAll('input[name=invoice]:checked')].map(i=>Number(i.value));
    if(!ids.length)return notify('请至少勾选一张到期账单');
    try{const r=await createWithdrawals(sel,ids);notify(`已登记 ${r.created_count} 张，去重 ${r.deduplicated_count} 张，等待 worker 生效`);await withdrawalsPage(el)}
    catch(err){notify(err.message)}
  };
}

function renderRegister(batchId,eligible,write){
  if(!eligible)return '<div class="empty">加载可撤下账单失败。</div>';
  if(eligible.error)return `<div class="panel"><h2>批次 #${batchId} 登记撤下</h2><p class="error">${esc(eligible.error)}</p></div>`;
  const avail=eligible.available_invoice_ids||[];
  return `<section class="panel"><div class="actions" style="margin-top:0"><h2 style="margin:0">批次 #${batchId} 登记撤下</h2>
      <a href="#evidence/${batchId}">打开该证据 →</a></div>
      <p class="helper">两侧都带账单号的账单 ${eligible.bilateral_invoice_ids.length} 张，其中已到期可撤下 ${eligible.eligible_invoice_ids.length} 张，已登记 ${eligible.already_registered.length} 张。</p>
      ${eligible.reason?('<p class="error">'+esc(eligible.reason)+'</p>'):''}
      ${write?(avail.length
        ?`<form id="register-form"><label>勾选到期账单（仅显示尚未登记的到期账单）</label>
          <div class="tablewrap"><table><thead><tr><th style="width:40px"></th><th>账单号</th><th>到期状态</th></tr></thead><tbody>
          ${avail.map(id=>`<tr><td><input type="checkbox" name="invoice" value="${id}" style="width:auto"></td><td class="mono">${id}</td><td><span class="badge failed">已到期</span></td></tr>`).join('')}
          </tbody></table></div>
          <div class="actions"><button class="primary" type="submit">登记撤下所选账单 →</button></div></form>`
        :'<p class="helper">该批次没有可新增登记的到期账单（可能均已登记或无到期行）。</p>')
        :'<p class="helper">观察员为只读，不能登记撤下。</p>'}
    </section>`;
}
