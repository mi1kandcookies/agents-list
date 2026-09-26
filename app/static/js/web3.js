// Agent's List - Web3 integration layer.
// Depends on: ethers v6 (UMD CDN), /config.js, contracts.js
//
// Payments: protected engagement and agent-task flows own authorization and
// screening. The legacy browser signer is intentionally disabled below.

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
      btn.innerHTML = `<span style="font-family:var(--font-num);font-variant-numeric:tabular-nums lining-nums">${short(window.AgentsList.address)}</span>`;
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

  // Legacy browser payment is intentionally unavailable. The browser must not
  // sign an authorization for the closed /api/x402/pay path; protected work is
  // initiated through the guided engagement or an agent-side x402 client with
  // the pre-sign screening hook.
  async function payWithX402() {
    throw new Error('legacy browser payment disabled; use the protected engagement flow');
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
