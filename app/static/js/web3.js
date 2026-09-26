// Agent's List - Web3 integration layer.
// Depends on: ethers v6 (UMD CDN), /config.js, contracts.js
//
// Payments: the buyer signs an EIP-3009 TransferWithAuthorization for USDC
// (EIP-712 domain read from the token contract by the server), and the
// platform facilitator submits it and pays gas via /api/x402/pay.

(() => {
  const CHAIN = window.AGENTSLIST_CHAIN || {};
  const ADDR = window.AGENTSLIST_ADDRESSES || {};
  const ABI = window.AGENTSLIST_ABIS || {};

  // ── STATE ────────────────────────────────────────────────────────────────
  window.AgentsList = {
    provider: null,
    signer: null,
    address: null,
    connected: false,
    contracts: {},
  };

  // ── HELPERS ──────────────────────────────────────────────────────────────
  const short = (a) => a ? a.slice(0, 6) + '...' + a.slice(-4) : '';
  const toUSDC = (n) => Number(n) / 1e6;
  const fromUSDC = (n) => BigInt(Math.round(Number(n) * 1e6));

  async function ensureChain() {
    if (!CHAIN.chainIdHex) return;
    const chainId = await window.ethereum.request({ method: 'eth_chainId' });
    if (chainId === CHAIN.chainIdHex) return;
    try {
      await window.ethereum.request({
        method: 'wallet_switchEthereumChain',
        params: [{ chainId: CHAIN.chainIdHex }],
      });
    } catch (e) {
      if (e.code !== 4902) throw e;
      await window.ethereum.request({
        method: 'wallet_addEthereumChain',
        params: [{
          chainId: CHAIN.chainIdHex,
          chainName: CHAIN.name,
          rpcUrls: [CHAIN.rpcUrl],
          nativeCurrency: CHAIN.nativeCurrency,
          blockExplorerUrls: [CHAIN.explorer],
        }],
      });
    }
  }

  // ── CONNECT ──────────────────────────────────────────────────────────────
  async function connectWallet() {
    if (!window.ethereum) {
      throw new Error('No browser wallet detected. Install MetaMask, Rabby or Coinbase Wallet.');
    }
    await window.ethereum.request({ method: 'eth_requestAccounts' });
    await ensureChain();

    const provider = new ethers.BrowserProvider(window.ethereum);
    const signer = await provider.getSigner();
    const address = await signer.getAddress();

    Object.assign(window.AgentsList, { provider, signer, address, connected: true, contracts: {} });
    for (const name of Object.keys(ADDR)) {
      if (ABI[name]) window.AgentsList.contracts[name] = new ethers.Contract(ADDR[name], ABI[name], signer);
    }

    updateWalletButton();
    if (window.showToast) showToast(`Connected: ${short(address)}`, 'success');
    window.dispatchEvent(new CustomEvent('agentslist:connected', { detail: { address } }));

    // Wallet-scoped pages read these cookies server-side.
    document.cookie = `buyer_wallet=${address}; path=/; max-age=${60 * 60 * 24 * 7}; SameSite=Lax`;
    document.cookie = `seller_wallet=${address}; path=/; max-age=${60 * 60 * 24 * 7}; SameSite=Lax`;
    const walletPages = ['/active-jobs', '/past-jobs', '/seller/earnings'];
    const path = window.location.pathname;
    if (walletPages.includes(path) && !new URLSearchParams(window.location.search).get('wallet')) {
      window.location.href = `${path}?wallet=${address}`;
    }
    return address;
  }

  function updateWalletButton() {
    const btn = document.getElementById('wallet-btn');
    if (!btn) return;
    if (window.AgentsList.connected) {
      btn.innerHTML = `<span style="font-family:var(--font-mono)">${short(window.AgentsList.address)}</span>`;
      btn.setAttribute('data-connected', 'true');
    } else {
      btn.innerHTML = '<span>Connect Wallet</span>';
      btn.setAttribute('data-connected', 'false');
    }
  }

  // ── READS ────────────────────────────────────────────────────────────────
  async function getUsdcBalance(address) {
    const who = address || window.AgentsList.address;
    if (!who || !ADDR.USDC) return 0n;
    try {
      const provider = window.AgentsList.provider || new ethers.JsonRpcProvider(CHAIN.rpcUrl);
      const usdc = new ethers.Contract(ADDR.USDC, ABI.USDC, provider);
      return await usdc.balanceOf(who);
    } catch (e) {
      return 0n;
    }
  }

  // ── x402 PAYMENT: sign EIP-3009 TransferWithAuthorization, submit via backend ──
  async function payWithX402({ agentId, depositUSDC, task = '' }) {
    // The nav can mark the wallet "connected" from a cookie without a signer.
    if (!window.AgentsList.signer) await connectWallet();
    const { signer, address } = window.AgentsList;

    const meta = await fetch('/api/x402/domain').then(r => r.json());
    const to = meta.recipient || window.AGENTSLIST_PAYMENT_RECIPIENT;
    if (!to) throw new Error('Payments are not configured on this server (PAYMENT_RECIPIENT unset).');

    const value = fromUSDC(depositUSDC);
    const validBefore = Math.floor(Date.now() / 1000) + 3600;
    const nonce = ethers.hexlify(crypto.getRandomValues(new Uint8Array(32)));
    const message = { from: address, to, value, validAfter: 0, validBefore, nonce };

    if (window.showToast) showToast('Sign the USDC payment authorization in your wallet...', 'info');
    const sig = await signer.signTypedData(meta.domain, meta.types, message);
    const { v, r, s } = ethers.Signature.from(sig);

    const res = await fetch('/api/x402/pay', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ...message, value: value.toString(), v, r, s, agentId, task }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `backend rejected payment (${res.status})`);
    return body;
  }

  // ── WIRE UP ──────────────────────────────────────────────────────────────
  function wireWallet() {
    const btn = document.getElementById('wallet-btn');
    if (!btn || btn.dataset.wired === 'true') return;
    btn.dataset.wired = 'true';
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wireWallet);
  else wireWallet();

  if (window.ethereum && window.ethereum.on) {
    try {
      window.ethereum.on('accountsChanged', () => window.location.reload());
      window.ethereum.on('chainChanged', () => window.location.reload());
    } catch (_) {}
  }

  // Expose API
  Object.assign(window.AgentsList, {
    connectWallet, getUsdcBalance, payWithX402, ensureChain, short, toUSDC, fromUSDC,
  });
})();
