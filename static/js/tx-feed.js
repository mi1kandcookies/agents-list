// Live on-chain activity feed. Polls recent events from Escrow + Reputation + Auction
// and renders them into any #live-tx-feed element on the page. No wallet required.

(() => {
  const POLL_INTERVAL = 12000;
  const MAX_EVENTS = 8;

  function short(a) { return a ? a.slice(0,6) + '...' + a.slice(-4) : ''; }
  function usdc(n) { return (Number(n)/1e6).toFixed(2); }
  function snowtraceTx(hash) { return window.AGENTHIRE_CHAIN.explorer + '/tx/' + hash; }

  async function pollEvents() {
    const feed = document.getElementById('live-tx-feed');
    if (!feed) return;
    try {
      const provider = new ethers.JsonRpcProvider(window.AGENTHIRE_CHAIN.rpcUrl);
      const latest = await provider.getBlockNumber();
      const fromBlock = latest - 1500; // ~50min of Fuji blocks

      const esc = new ethers.Contract(window.AGENTHIRE_ADDRESSES.EscrowPayment, [
        'event PaymentSettled(uint256 indexed sessionId, address indexed agent, uint256 amount)',
        'event FundsDeposited(uint256 indexed sessionId, uint256 indexed agentId, address indexed user, uint256 amount, uint256 pricePerToken, uint256 categoryId, uint64 expiresAt)',
      ], provider);
      const rep = new ethers.Contract(window.AGENTHIRE_ADDRESSES.ReputationContract, [
        'event TaskCompleted(uint256 indexed agentId, uint256 indexed sessionId, uint256 tokensUsed, uint256 volume, uint256 categoryId, uint256 pointsAwarded)',
        'event IncidentRecorded(uint256 indexed agentId, address indexed affectedUser, uint8 severity)',
      ], provider);
      const auc = new ethers.Contract(window.AGENTHIRE_ADDRESSES.AuctionMarket, [
        'event BidPosted(uint256 indexed bidId, address indexed user, uint256 depositAmount, uint256 tokenBudget, uint256 maxPricePerToken, uint256 categoryId, uint8 minTier, uint64 expiresAt)',
        'event BidClaimed(uint256 indexed bidId, uint256 indexed agentId, uint256 tokensUsed, uint256 agentPayment)',
      ], provider);

      const [deposited, settled, completed, incidents, bidPosted, bidClaimed] = await Promise.all([
        esc.queryFilter('FundsDeposited', fromBlock).catch(() => []),
        esc.queryFilter('PaymentSettled', fromBlock).catch(() => []),
        rep.queryFilter('TaskCompleted', fromBlock).catch(() => []),
        rep.queryFilter('IncidentRecorded', fromBlock).catch(() => []),
        auc.queryFilter('BidPosted', fromBlock).catch(() => []),
        auc.queryFilter('BidClaimed', fromBlock).catch(() => []),
      ]);

      const all = [];
      for (const e of deposited) all.push({
        block: e.blockNumber, tx: e.transactionHash,
        icon: '💰', color: 'var(--green)',
        text: `Deposit ${usdc(e.args.amount)} USDC → agent #${e.args.agentId} (session ${e.args.sessionId})`,
      });
      for (const e of settled) all.push({
        block: e.blockNumber, tx: e.transactionHash,
        icon: '✓', color: 'var(--green)',
        text: `Paid ${usdc(e.args.amount)} USDC to ${short(e.args.agent)} (session ${e.args.sessionId})`,
      });
      for (const e of completed) all.push({
        block: e.blockNumber, tx: e.transactionHash,
        icon: '★', color: '#7fb3ff',
        text: `Agent #${e.args.agentId} +${e.args.pointsAwarded} reputation (${usdc(e.args.volume)} USDC volume)`,
      });
      for (const e of incidents) all.push({
        block: e.blockNumber, tx: e.transactionHash,
        icon: '!', color: e.args.severity === 2 ? '#ff7f7f' : 'var(--amber)',
        text: `Incident: agent #${e.args.agentId} sev=${e.args.severity}`,
      });
      for (const e of bidPosted) all.push({
        block: e.blockNumber, tx: e.transactionHash,
        icon: '⚡', color: 'var(--amber)',
        text: `Open bid #${e.args.bidId}: ${usdc(e.args.depositAmount)} USDC (tier ≥ ${e.args.minTier})`,
      });
      for (const e of bidClaimed) all.push({
        block: e.blockNumber, tx: e.transactionHash,
        icon: '⚡',  color: 'var(--green)',
        text: `Bid #${e.args.bidId} claimed by agent #${e.args.agentId} (${usdc(e.args.agentPayment)} USDC)`,
      });

      all.sort((a,b) => b.block - a.block);
      if (all.length) {
        feed.innerHTML = all.slice(0, MAX_EVENTS).map(e => `
          <a href="${snowtraceTx(e.tx)}" target="_blank" style="display:flex; align-items:center; gap:10px; padding:8px 10px; border-bottom:1px solid var(--border-2); color:var(--text-1); text-decoration:none; font-family:var(--font-mono); font-size:.6875rem;">
            <span style="color:${e.color}; width:14px; text-align:center;">${e.icon}</span>
            <span style="flex:1;">${e.text}</span>
            <span style="color:var(--text-3); font-size:.625rem;">block ${e.block}</span>
          </a>
        `).join('');
      } else {
        await renderSimulated(feed);
      }
    } catch (e) {
      console.warn('tx feed (chain) failed:', e);
      try { await renderSimulated(feed); } catch (_) {}
    }
  }

  // Fallback: render the simulated activity feed from the Flask backend.
  // Keeps the UI alive when the Fuji RPC returns zero recent events or no
  // signer keys are configured.
  async function renderSimulated(feed) {
    const res = await fetch('/api/transactions?limit=' + MAX_EVENTS);
    if (!res.ok) return;
    const { transactions } = await res.json();
    if (!transactions || !transactions.length) {
      feed.innerHTML = `<div style="padding:14px; color:var(--text-3); font-family:var(--font-mono); font-size:.75rem;">No recent on-chain activity.</div>`;
      return;
    }
    const KIND = {
      deposit:  { icon: '$',  color: 'var(--green)',  verb: 'Deposit' },
      settle:   { icon: '✓', color: 'var(--green)',  verb: 'Settled'  },
      refund:   { icon: '↩', color: 'var(--amber)',  verb: 'Refund'   },
      stake:    { icon: '▲', color: '#7fb3ff',       verb: 'Stake'    },
      unstake:  { icon: '▼', color: 'var(--amber)',  verb: 'Unstake'  },
      slash:    { icon: '!', color: '#ff7f7f',       verb: 'Slash'    },
      register: { icon: '◆', color: 'var(--text-2)', verb: 'Register' },
      bid_post: { icon: '⚡', color: 'var(--amber)',  verb: 'Bid'      },
      bid_claim:{ icon: '⚡', color: 'var(--green)',  verb: 'Claim'    },
    };
    feed.innerHTML = transactions.map(tx => {
      const k = KIND[tx.kind] || { icon: '•', color: 'var(--text-3)', verb: tx.kind };
      const amt = tx.amountUSDCDisplay ? '$' + Number(tx.amountUSDCDisplay).toFixed(2) + ' USDC' : '';
      const short = tx.txHash ? tx.txHash.slice(0,8) + '…' + tx.txHash.slice(-4) : '';
      return `<a href="#" onclick="return false;" style="display:flex; align-items:center; gap:10px; padding:8px 10px; border-bottom:1px solid var(--border-2); color:var(--text-1); text-decoration:none; font-family:var(--font-mono); font-size:.6875rem;">
        <span style="color:${k.color}; width:14px; text-align:center;">${k.icon}</span>
        <span style="flex:1;">${k.verb} · agent #${tx.agentId || '-'} · ${amt}</span>
        <span style="color:var(--text-3); font-size:.625rem;">${short}</span>
      </a>`;
    }).join('');
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (!document.getElementById('live-tx-feed')) return;
    pollEvents();
    setInterval(pollEvents, POLL_INTERVAL);
  });
})();
