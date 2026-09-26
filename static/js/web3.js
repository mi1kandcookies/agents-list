// AgentHire - Web3 integration layer.
// Depends on: ethers (UMD CDN), contracts.js

(() => {
  const CHAIN = window.AGENTHIRE_CHAIN;
  const ADDR = window.AGENTHIRE_ADDRESSES;
  const ABI = window.AGENTHIRE_ABIS;

  // ── STATE ────────────────────────────────────────────────────────────────
  window.AgentHire = {
    provider: null,
    signer: null,
    address: null,
    connected: false,
    contracts: {},
  };

  // ── HELPERS ──────────────────────────────────────────────────────────────
  const short = (a) => a ? a.slice(0, 6) + '...' + a.slice(-4) : '';
  const toUSDC = (n) => Number(n) / 1e6;
  const fromUSDC = (n) => BigInt(Math.floor(Number(n) * 1e6));

  async function ensureFuji() {
    const chainId = await window.ethereum.request({ method: 'eth_chainId' });
    if (chainId !== CHAIN.chainIdHex) {
      try {
        await window.ethereum.request({
          method: 'wallet_switchEthereumChain',
          params: [{ chainId: CHAIN.chainIdHex }],
        });
      } catch (e) {
        if (e.code === 4902) {
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
        } else { throw e; }
      }
    }
  }

  // ── DEMO MODE (no wallet detected) ────────────────────────────────────────
  // Activates a read-only demo session with a mock address so the UI
  // renders connected state without requiring an actual wallet extension.
  function activateDemoMode() {
    // Valid 0x-prefixed 40-hex address so backend address validation passes.
    const DEMO_ADDRESS = '0xDeAD0000000000000000000000000000D3Mo0001';
    window.AgentHire.address = DEMO_ADDRESS;
    window.AgentHire.connected = true; // visual only - no signing capability
    window.AgentHire._demoMode = true;
    const btn = document.getElementById('wallet-btn');
    if (btn) {
      btn.innerHTML = `<span style="font-family:var(--font-mono); color:var(--amber);">Demo Mode</span>`;
      btn.setAttribute('data-connected', 'true');
      btn.title = 'Demo mode - no real wallet connected. Payments will use mock responses.';
    }
    if (window.showToast) showToast('Demo mode active - no wallet required', 'info');
    window.dispatchEvent(new CustomEvent('agenthire:connected', { detail: { address: DEMO_ADDRESS, demo: true } }));
  }

  // ── CONNECT ──────────────────────────────────────────────────────────────
  async function connectWallet() {
    if (!window.ethereum) {
      // No wallet extension detected - offer demo mode
      if (window.showToast) {
        showToast('No wallet detected - switching to Demo Mode', 'warning');
      }
      activateDemoMode();
      return window.AgentHire.address;
    }
    await window.ethereum.request({ method: 'eth_requestAccounts' });
    await ensureFuji();

    const provider = new ethers.BrowserProvider(window.ethereum);
    const signer = await provider.getSigner();
    const address = await signer.getAddress();

    window.AgentHire.provider = provider;
    window.AgentHire.signer = signer;
    window.AgentHire.address = address;
    window.AgentHire.connected = true;

    // Instantiate all contracts
    for (const name of Object.keys(ADDR)) {
      window.AgentHire.contracts[name] = new ethers.Contract(ADDR[name], ABI[name], signer);
    }

    updateWalletButton();
    if (window.showToast) showToast(`Connected: ${short(address)}`, 'success');
    window.dispatchEvent(new CustomEvent('agenthire:connected', { detail: { address } }));

    // Persist address so buyer/seller pages load this wallet's on-chain activity.
    // /active-jobs, /past-jobs, /seller/earnings all read the buyer_wallet/seller_wallet
    // cookie so you don't have to paste your address into a query param after connecting.
    document.cookie = `buyer_wallet=${address}; path=/; max-age=${60*60*24*7}; SameSite=Lax`;
    document.cookie = `seller_wallet=${address}; path=/; max-age=${60*60*24*7}; SameSite=Lax`;

    // If we're on a page that renders wallet-scoped data, redirect with the
    // wallet param so the server-side render picks up this address.
    const wallet_pages = ['/active-jobs', '/past-jobs', '/seller/earnings'];
    const path = window.location.pathname;
    if (wallet_pages.includes(path) && !new URLSearchParams(window.location.search).get('wallet')) {
      window.location.href = `${path}?wallet=${address}`;
    }

    return address;
  }

  function updateWalletButton() {
    const btn = document.getElementById('wallet-btn');
    if (!btn) return;
    if (window.AgentHire.connected) {
      btn.innerHTML = `<span style="font-family:var(--font-mono)">${short(window.AgentHire.address)}</span>`;
      btn.setAttribute('data-connected', 'true');
    } else {
      btn.innerHTML = '<span>Connect Wallet</span>';
      btn.setAttribute('data-connected', 'false');
    }
  }

  // ── READS ────────────────────────────────────────────────────────────────
  async function getUsdcBalance() {
    if (!window.AgentHire.connected) return 0n;
    // If we're in demo-wallet mode (connected via inline handler, no ethers
    // contract bound), instantiate a read-only contract against the public RPC
    // so balance reads still work without a browser wallet extension.
    if (!window.AgentHire.contracts || !window.AgentHire.contracts.MockUSDC) {
      try {
        const provider = new ethers.JsonRpcProvider(CHAIN.rpcUrl);
        const c = new ethers.Contract(ADDR.MockUSDC, ABI.MockUSDC, provider);
        return await c.balanceOf(window.AgentHire.address);
      } catch (e) { return 0n; }
    }
    return window.AgentHire.contracts.MockUSDC.balanceOf(window.AgentHire.address);
  }

  async function getAgentProfile(agentId) {
    // Read-only - works without connect via public RPC
    const provider = window.AgentHire.provider || new ethers.JsonRpcProvider(CHAIN.rpcUrl);
    const reg = new ethers.Contract(ADDR.AgentRegistry, ABI.AgentRegistry, provider);
    const rep = new ethers.Contract(ADDR.ReputationContract, ABI.ReputationContract, provider);
    const stake = new ethers.Contract(ADDR.StakingSlashing, ABI.StakingSlashing, provider);

    const [agent, listing, profile, stakeInfo] = await Promise.all([
      reg.getAgent(agentId),
      reg.getListing(agentId),
      rep.getCreditProfile(agentId),
      stake.getStake(agentId),
    ]);
    return {
      id: Number(agentId),
      wallet: agent.wallet,
      name: agent.name,
      endpoint: agent.endpointURL,
      active: agent.active,
      banned: agent.banned,
      listing: {
        minPricePerToken: listing.minPricePerToken.toString(),
        maxTokensPerSession: listing.maxTokensPerSession.toString(),
        acceptingWork: listing.acceptingWork,
      },
      reputation: {
        score: Number(profile.score),
        tier: Number(profile.tier),
        tasks: Number(profile.tasksCompleted),
        incidents: Number(profile.incidentCount),
        projectedScore: Number(profile.projectedScore),
      },
      stake: {
        amountUSDC: toUSDC(stakeInfo[0]),
        incidents: Number(stakeInfo[1]),
        banned: stakeInfo[2],
      },
    };
  }

  // ── MINT USDC (testnet only) ─────────────────────────────────────────────
  async function mintUSDC(amountUSDC = 1000) {
    if (!window.AgentHire.connected) await connectWallet();
    const amt = fromUSDC(amountUSDC);
    const tx = await window.AgentHire.contracts.MockUSDC.mint(window.AgentHire.address, amt);
    if (window.showToast) showToast(`Minting ${amountUSDC} USDC...`, 'info');
    await tx.wait();
    if (window.showToast) showToast(`+${amountUSDC} USDC`, 'success');
    return tx.hash;
  }

  // ── x402 PAYMENT - sign EIP-3009, call backend, open escrow ──────────────
  async function payWithX402({ agentId, depositUSDC, tokenBudget, categoryId = 0, facilitator }) {
    if (!window.AgentHire.connected) await connectWallet();
    const { signer, address } = window.AgentHire;
    const value = fromUSDC(depositUSDC);
    const now = Math.floor(Date.now() / 1000);
    const validBefore = now + 3600;
    const nonce = '0x' + Array.from(crypto.getRandomValues(new Uint8Array(32)))
      .map(b => b.toString(16).padStart(2, '0')).join('');

    // EIP-712 domain for MockUSDC
    const domain = {
      name: 'Mock USDC',
      version: '1',
      chainId: CHAIN.chainId,
      verifyingContract: ADDR.MockUSDC,
    };
    const types = {
      TransferWithAuthorization: [
        { name: 'from', type: 'address' },
        { name: 'to', type: 'address' },
        { name: 'value', type: 'uint256' },
        { name: 'validAfter', type: 'uint256' },
        { name: 'validBefore', type: 'uint256' },
        { name: 'nonce', type: 'bytes32' },
      ],
    };
    const msg = {
      from: address,
      to: facilitator,
      value,
      validAfter: 0,
      validBefore,
      nonce,
    };

    if (window.showToast) showToast('Sign the payment permit in your wallet...', 'info');
    const sig = await signer.signTypedData(domain, types, msg);
    const { v, r, s } = ethers.Signature.from(sig);

    // POST to backend facilitator endpoint.
    // Backend is expected to call transferWithAuthorization + depositFunds.
    const body = {
      ...msg,
      value: value.toString(),
      v, r, s,
      agentId,
      tokenBudget: String(tokenBudget),
      categoryId,
    };
    const res = await fetch('/api/x402/pay', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'unknown' }));
      throw new Error(err.error || `backend rejected payment (${res.status})`);
    }
    return res.json();
  }

  // ── DIRECT ESCROW DEPOSIT (non-x402 fallback) ────────────────────────────
  async function depositDirect({ agentId, depositUSDC, tokenBudget, categoryId = 0 }) {
    if (!window.AgentHire.connected) await connectWallet();
    const amt = fromUSDC(depositUSDC);
    const expiresAt = Math.floor(Date.now() / 1000) + 3600;
    if (window.showToast) showToast('Approving USDC spend...', 'info');
    const tx1 = await window.AgentHire.contracts.MockUSDC.approve(ADDR.EscrowPayment, amt);
    await tx1.wait();
    if (window.showToast) showToast('Opening escrow session...', 'info');
    const tx2 = await window.AgentHire.contracts.EscrowPayment.depositFunds(
      agentId, amt, BigInt(tokenBudget), categoryId, expiresAt
    );
    const rc = await tx2.wait();
    if (window.showToast) showToast('Session opened on-chain', 'success');
    return { txHash: tx2.hash, blockNumber: rc.blockNumber };
  }

  // ── WIRE UP GLOBAL HANDLERS ──────────────────────────────────────────────
  function _wireWallet() {
    const btn = document.getElementById('wallet-btn');
    if (!btn) { console.warn('[agenthire] wallet-btn not in DOM on this page'); return; }
    if (btn.dataset.wired === 'true') return;
    btn.dataset.wired = 'true';
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      console.log('[agenthire] wallet-btn clicked');
      if (window.AgentHire.connected) { console.log('[agenthire] already connected, ignoring click'); return; }
      if (!window.ethereum) {
        const msg = 'No wallet extension detected. Install MetaMask, Rabby, or Coinbase Wallet and refresh.';
        console.error('[agenthire]', msg);
        if (window.showToast) showToast(msg, 'error');
        else alert(msg);
        return;
      }
      try {
        await connectWallet();
      } catch (err) {
        const msg = err && err.message ? err.message : String(err);
        console.error('[agenthire] connect failed:', err);
        if (window.showToast) showToast('Connect failed: ' + msg, 'error');
        else alert('Connect failed: ' + msg);
      }
    });
    console.log('[agenthire] wallet-btn wired');
  }

  // Try wiring immediately and again on DOM ready — some pages (landing)
  // include this script at the bottom of body so DOMContentLoaded may have
  // already fired before we get here.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _wireWallet);
  } else {
    _wireWallet();
  }

  // Restore state + listen for wallet events once everything else is ready.
  (function _setupEthereumListeners() {
    function go() {
      if (window.ethereum && window.ethereum.selectedAddress) {
        connectWallet().catch((e) => console.warn('[agenthire] auto-restore failed:', e));
      }
      if (window.ethereum) {
        try {
          window.ethereum.on('accountsChanged', () => window.location.reload());
          window.ethereum.on('chainChanged', () => window.location.reload());
        } catch (_) {}
      }
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go);
    else go();
  })();

  // Expose API
  window.AgentHire.connectWallet = connectWallet;
  window.AgentHire.activateDemoMode = activateDemoMode;
  window.AgentHire.getUsdcBalance = getUsdcBalance;
  window.AgentHire.getAgentProfile = getAgentProfile;
  window.AgentHire.mintUSDC = mintUSDC;
  window.AgentHire.payWithX402 = payWithX402;
  window.AgentHire.depositDirect = depositDirect;
  window.AgentHire.short = short;
  window.AgentHire.toUSDC = toUSDC;
  window.AgentHire.fromUSDC = fromUSDC;
})();
