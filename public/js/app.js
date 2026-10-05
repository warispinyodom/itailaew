const App = (() => {
  const state = { user: null, token: localStorage.getItem("itailaew_token"), refreshToken: localStorage.getItem("itailaew_refresh_token"), authMode: "login", menuPage: 1, polling: null, presenceTimer: null, tableVerified: false, selectedTable: null, pendingTable: null, cart: {}, hasActiveOrder: false, hadActiveOrder: false, submittingCart: false };
  const $ = id => document.getElementById(id);

  async function api(path, options = {}) {
    const headers = {"Content-Type":"application/json", ...(options.headers || {})};
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const res = await fetch(path, {...options, headers});
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || data.ok === false) { const msg = typeof data.message === "string" ? data.message : "เกิดข้อผิดพลาด"; const err = new Error(msg); err.payload = data.message; throw err; }
    return data;
  }

  function toast(message) {
    const el = $("toast"); el.textContent = message; el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 2800);
  }

  function go(id) {
    const protectedPages = ["home","menu","reservation","customer","table-select","cart","admin","staff","tables","pos"];
    if (state.user?.role === "customer" && !selectedTable() && ["menu","cart","customer"].includes(id)) id = "table-select";
    if (protectedPages.includes(id) && !state.user) {
      document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
      $("login")?.classList.add("active");
      refreshNav();
      toast("กรุณา Login ก่อนจึงจะเข้าเว็บไซต์ได้");
      return;
    }
    if (id === "login" && state.user) {
      id = state.user.role === "admin" ? "admin" : state.user.role === "staff" ? "staff" : "home";
    }
    if (id === "admin" && state.user?.role !== "admin") { id = "home"; toast("ไม่มีสิทธิ์เข้าถึง"); }
    if ((id === "staff" || id === "tables" || id === "pos") && !["admin","staff"].includes(state.user?.role)) { id = "home"; toast("ไม่มีสิทธิ์เข้าถึง"); }
    // Admin only sees the admin area (no customer-facing Home / Menu / Reservation)
    if (state.user?.role === "admin" && ["home","menu","reservation","customer"].includes(id)) id = "admin";
    // Staff only sees the staff area (customer pages are not for staff)
    if (state.user?.role === "staff" && ["home","menu","reservation","customer"].includes(id)) id = "staff";
    if (state.polling) { clearInterval(state.polling); state.polling = null; }
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    $(id)?.classList.add("active");
    if (id !== "login") localStorage.setItem("itailaew_current_page", id);
    if (id === "home") loadRecommendedMenus();
    if (id === "table-select") { loadCustomerTables(); const back=$("table-back"); if(back) back.classList.toggle("hidden", !!selectedTable()); }
    if (id === "menu") loadMenus();
    if (id === "cart") { renderCart(); }
    if (id === "reservation") { setReservationMinimum(); loadReservations(); }
    if (id === "customer") { loadCustomer(); state.polling = setInterval(loadCustomer, 8000); }
    if (id === "admin") loadDashboard();
    if (id === "staff") { loadStaff(); state.polling = setInterval(loadStaff, 8000); }
    if (id === "tables") { loadTables(); state.polling = setInterval(loadTables, 8000); }
    if (id === "pos") loadPos();
    window.scrollTo({top:0,behavior:"smooth"});
  }

  function authMode(mode) {
    state.authMode = mode;
    $("name-label").classList.toggle("hidden", mode === "login");
    $("auth-title").textContent = mode === "login" ? "Welcome back" : "Create your account";
    document.querySelectorAll(".tab").forEach((x,i) => x.classList.toggle("active", (mode==="login"&&i===0)||(mode==="register"&&i===1)));
  }

  async function submitAuth(event) {
    event.preventDefault();
    try {
      const mode = state.authMode;
      const body = {email:$("auth-email").value, password:$("auth-password").value};
      if (mode === "register") body.name = $("auth-name").value;
      const data = await api(`/api/auth/${mode === "login" ? "login" : "register"}`, {method:"POST",body:JSON.stringify(body)});
      state.token = data.token; state.refreshToken = data.refresh_token || state.refreshToken; state.user = data.user; localStorage.setItem("itailaew_token", state.token); if(state.refreshToken)localStorage.setItem("itailaew_refresh_token", state.refreshToken); loadUserStorage(); validateSelectedTableForUser();
      refreshNav(); toast(data.message);
      if (state.user.role === "admin") go("admin"); else if (state.user.role === "staff") go("staff"); else go("home");
    } catch(e) { toast(e.message); }
  }

  function refreshNav() {
    const loggedIn = !!state.user;
    $("main-nav").classList.toggle("hidden", !loggedIn);
    $("login-nav").classList.toggle("hidden", loggedIn);
    $("logout-nav").classList.toggle("hidden", !loggedIn);
    document.querySelectorAll("#main-nav button[data-roles]").forEach(b => {
      const roles = b.dataset.roles.split(",");
      const roleVisible = loggedIn && roles.includes(state.user.role);
      const tableVisible = !b.hasAttribute("data-requires-table") || !!selectedTable();
      b.classList.toggle("hidden", !(roleVisible && tableVisible));
    });
    const isCustomer = loggedIn && state.user.role === "customer";
    const reservationNav = $("reservation-nav");
    if (reservationNav) reservationNav.classList.add("hidden");
    const tableNav = $("table-nav");
    if (tableNav) tableNav.classList.add("hidden");
    ["menu-nav","cart-nav"].forEach(id => {
      const button = $(id);
      if (button) button.classList.toggle("hidden", !(isCustomer && !!selectedTable()));
    });
    if (isCustomer && !selectedTable()) {
      ["table-nav", "reservation-nav", "menu-nav", "cart-nav", "bill-nav"].forEach(id => $(id)?.classList.add("hidden"));
    }
    const billButton = $("bill-nav");
    if (billButton) billButton.classList.toggle("hidden", !(isCustomer && !!selectedTable() && state.hasActiveOrder));
    $("customer-orders-back")?.classList.toggle("hidden", !(isCustomer && state.hasActiveOrder));
  }

  async function logout() {
    if (state.user?.role === "customer" && state.token) {
      try { await api("/api/customer/leave", {method:"POST", body:JSON.stringify({reason:"logout"})}); } catch (_) { }
    }
    if (state.polling) { clearInterval(state.polling); state.polling = null; }
    stopPresence(); state.user = null; state.token = null; state.refreshToken = null; state.tableVerified=false; state.hadActiveOrder=false; state.hasActiveOrder=false; localStorage.removeItem("itailaew_token"); localStorage.removeItem("itailaew_refresh_token"); localStorage.removeItem("itailaew_current_page"); state.selectedTable=null; state.cart={};
    refreshNav();
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    $("login")?.classList.add("active");
    authMode("login");
    toast("ออกจากระบบแล้ว");
  }

  async function restore() {
    refreshNav();
    if (state.qrTable && $("qr-banner")) {
      $("qr-banner").classList.remove("hidden");
      $("qr-banner").innerHTML = `<b>🍽️ itailaew — TABLE ${escapeHtml(state.qrTable)}</b><div class="small">กำลังสั่งจากโต๊ะนี้ ออเดอร์จะส่งเข้าครัวทันที</div>`;
    }
    if (!state.token) {
      document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
      $("login")?.classList.add("active");
      authMode("login");
      return;
    }
    try {
      let data;
      try {
        data = await api("/api/me");
      } catch (firstError) {
        if (!state.refreshToken) throw firstError;
        const renewed = await api("/api/auth/refresh", {method:"POST", body:JSON.stringify({refresh_token:state.refreshToken})});
        state.token = renewed.token;
        state.refreshToken = renewed.refresh_token || state.refreshToken;
        localStorage.setItem("itailaew_token", state.token);
        localStorage.setItem("itailaew_refresh_token", state.refreshToken);
        data = {user: renewed.user};
      }
      state.user = data.user;
      loadUserStorage();
      state.tableVerified = false;
      validateSelectedTableForUser();
      if (state.user.role === "customer" && state.selectedTable) {
        try {
          const moves = await api("/api/customer/table-move-requests");
          const completed = moves.requests.find(r => r.status === "completed");
          const tables = await api("/api/customer/tables");
          if (completed) {
            const moved = tables.tables.find(t => t.id === completed.new_table_id && t.claimed_by === state.user.id);
            if (moved) { state.selectedTable = moved; localStorage.setItem(tableKey(), JSON.stringify(moved)); }
          }
          const live = tables.tables.find(t => t.id === state.selectedTable?.id);
          if (!live || !tableHasCurrentUser(live)) clearCustomerSessionData();
          else { state.selectedTable = live; state.tableVerified = true; startPresence(); }
        } catch (_) { state.selectedTable=null; state.tableVerified=false; }
      }
      // A refresh must recover the table from Firebase, not only from a stale
      // localStorage snapshot. This also lets a full table recognize its
      // existing member instead of showing it as a new join attempt.
      if (state.user.role === "customer" && !state.selectedTable) {
        try {
          const tables = await api("/api/customer/tables");
          const mine = tables.tables.find(t => tableHasCurrentUser(t));
          if (mine) {
            state.selectedTable = mine;
            state.tableVerified = true;
            localStorage.setItem(tableKey(), JSON.stringify(mine));
            startPresence();
          }
        } catch (_) { }
      }
      if (state.user.role === "customer" && state.tableVerified && state.selectedTable) {
        // Rehydrate active-order state before refreshNav; otherwise the Bill
        // button disappears after a hard refresh even though the order exists.
        try { await loadCustomer(); } catch (_) { }
      }
      refreshNav();
      const savedPage = localStorage.getItem("itailaew_current_page");
      const allowed = ["home","menu","reservation","customer","table-select","cart","admin","staff","tables","pos"];
      let target = allowed.includes(savedPage) ? savedPage : (state.user.role === "admin" ? "admin" : state.user.role === "staff" ? "staff" : "home");
      if (state.user.role === "customer" && !state.selectedTable && ["menu","cart","customer"].includes(target)) target = "table-select";
      if (state.user.role === "admin") target = ["admin","tables","pos"].includes(target) ? target : "admin";
      if (state.user.role === "staff") target = ["staff","tables","pos"].includes(target) ? target : "staff";
      document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
      $(target)?.classList.add("active");
      localStorage.setItem("itailaew_current_page", target);
      if (target === "admin") loadDashboard();
      if (target === "staff") { loadStaff(); if (!state.polling) state.polling = setInterval(loadStaff, 8000); }
      if (target === "table-select") loadCustomerTables();
      if (target === "menu") loadMenus();
      if (target === "reservation") { setReservationMinimum(); loadReservations(); }
      if (target === "customer") { loadCustomer(); if (!state.polling) state.polling = setInterval(loadCustomer, 8000); }
      if (target === "cart") renderCart();
      if (target === "tables") { loadTables(); if (!state.polling) state.polling = setInterval(loadTables, 8000); }
      if (target === "pos") loadPos();
    } catch (_) {
      // Do not destroy a valid local identity because a secondary page/data
      // request failed during boot. Only clear auth when /api/me and refresh
      // token recovery both failed before a user was established.
      if (state.user) { refreshNav(); return; }
      state.tableVerified=false;
      await logout();
    }
  }


  async function loadMenus(page = state.menuPage) {
    try {
      state.menuPage = page;
      const q = encodeURIComponent($("menu-search")?.value || "");
      const sort = $("menu-sort")?.value || "name";
      const category = encodeURIComponent($("menu-category")?.value || "");
      const data = await api(`/api/menus?q=${q}&category=${category}&sort=${sort}&page=${page}&page_size=8`);
      state.menus = data.items;
      const grid = $("menu-grid");
      grid.innerHTML = data.items.map(m => `<article class="menu-card"><div class="menu-image">${m.image_url ? `<img src="${escapeHtml(m.image_url)}" style="width:100%;height:100%;object-fit:cover">` : (m.category==="Pizza"?"🍕":m.category==="Dessert"?"🍰":"🍝")}</div><div class="menu-body"><h3>${escapeHtml(m.name)}</h3><p>${escapeHtml(m.category)}${m.is_out_of_stock ? ' <span class="sold">หมด</span>':''}</p><span class="price">฿${Number(m.price).toFixed(2)}</span>${state.user?.role === "customer" ? `<button type="button" class="secondary" style="float:right;padding:7px 12px" ${m.is_out_of_stock || !selectedTable()?"disabled":""} onclick="App.quickOrder('${m.id}')">+</button>`:""}</div></article>`).join("") || `<div class="list-card">ยังไม่มีเมนู</div>`;
      $("menu-pages").innerHTML = Array.from({length:data.total_pages},(_,i)=>`<button onclick="App.loadMenus(${i+1})">${i+1}</button>`).join("");
    } catch(e) { $("menu-grid").innerHTML = `<div class="list-card">${escapeHtml(e.message)}</div>`; }
  }

  async function loadRecommendedMenus(){
    const grid=$("recommended-menu-grid"); if(!grid)return;
    try{const data=await api("/api/recommended-menus");grid.innerHTML=data.items.map(m=>`<article class="menu-card"><div class="menu-image">${m.image_url?`<img src="${escapeHtml(m.image_url)}" style="width:100%;height:100%;object-fit:cover">`:(m.category==="Pizza"?"🍕":m.category==="Dessert"?"🍰":"🍝")}</div><div class="menu-body"><h3>${escapeHtml(m.name)}</h3><p>${escapeHtml(m.category)} · สั่งแล้ว ${Number(m.ordered_count)} ครั้ง</p><span class="price">฿${Number(m.price).toFixed(2)}</span></div></article>`).join("")||`<div class="list-card">ยังไม่มีข้อมูลเมนูยอดนิยม</div>`}catch(e){grid.innerHTML=`<div class="list-card">${escapeHtml(e.message)}</div>`}
  }

  function userKey(){ return state.user?.id ? String(state.user.id).replace(/[^a-zA-Z0-9_-]/g,"_") : "anonymous"; }
  function cartKey(){ return `itailaew_cart_${userKey()}`; }
  function tableKey(){ return `itailaew_selected_table_${userKey()}`; }
  function loadUserStorage(){ if(!state.user)return; try{state.cart=JSON.parse(localStorage.getItem(cartKey())||"{}");}catch(_){state.cart={};} try{state.selectedTable=JSON.parse(localStorage.getItem(tableKey())||"null");}catch(_){state.selectedTable=null;} state.tableVerified=false; }
  function persistCart(){ if(state.user) localStorage.setItem(cartKey(), JSON.stringify(state.cart||{})); }
  function selectedTable(){ return state.tableVerified ? state.selectedTable : null; }
  function stopPresence(){ if(state.presenceTimer){clearInterval(state.presenceTimer);state.presenceTimer=null;} }
  function startPresence(){
    stopPresence();
    if(state.user?.role!=="customer" || !state.tableVerified || !state.selectedTable) return;
    const beat=()=>api("/api/customer/presence",{method:"POST",body:JSON.stringify({table_id:state.selectedTable.id})}).catch(()=>{});
    beat(); state.presenceTimer=setInterval(beat,20000);
  }
  async function syncLiveCustomerTable(){
    if(state.user?.role!=="customer") return null;
    const d=await api("/api/customer/tables");
    const mine=d.tables.find(t=>tableHasCurrentUser(t));
    if(mine){state.selectedTable=mine;state.tableVerified=true;localStorage.setItem(tableKey(),JSON.stringify(mine));startPresence();refreshNav();}
    else {state.selectedTable=null;state.tableVerified=false;localStorage.removeItem(tableKey());stopPresence();refreshNav();}
    return mine||null;
  }
  function clearSelectedTable(){ state.selectedTable=null; state.tableVerified=false; stopPresence(); if(state.user)localStorage.removeItem(tableKey()); refreshNav(); }
  function clearCustomerSessionData(){ state.cart={}; state.selectedTable=null; state.tableVerified=false; state.hadActiveOrder=false; stopPresence(); if(state.user){localStorage.removeItem(cartKey());localStorage.removeItem(tableKey());} refreshNav(); }
  function tableHasCurrentUser(table){ return !!(table && state.user && ((Array.isArray(table.occupant_ids) && table.occupant_ids.includes(state.user.id)) || table.claimed_by === state.user.id)); }
  function validateSelectedTableForUser(){ if(state.user?.role === "customer" && state.selectedTable && !tableHasCurrentUser(state.selectedTable)) clearSelectedTable(); }
  async function quickOrder(menuId){
    if(!state.user) return go("login"); if(state.user.role!=="customer") return toast("ฟังก์ชันนี้สำหรับ Customer"); if(!selectedTable()) return go("table-select");
    const menu=state.menus.find(x=>x.id===menuId); if(!menu){ toast("ไม่พบเมนู กรุณาโหลดหน้าใหม่"); return; }
    const optionFields=Object.entries(menu.options||{}).map(([key,values])=>`<label>${escapeHtml(key)}<select id="opt-${escapeAttr(key)}">${(Array.isArray(values)?values:[]).map(v=>{const name=typeof v==="object"?v.name:v;const price=typeof v==="object"?Number(v.price||0):0;return `<option value="${escapeAttr(name)}">${escapeHtml(name)}${price?` (+฿${price.toFixed(2)})`:""}</option>`}).join("")}</select></label>`).join("");
    openModal(`<h2>${escapeHtml(menu.name)}</h2><p class="small">เลือกรายละเอียดอาหารก่อนเพิ่มลงตะกร้า</p>${optionFields}<label>หมายเหตุเพิ่มเติม<span style="display:block"></span><textarea id="menu-note" rows="3" style="display:block;width:100%;margin-top:7px" placeholder="เช่น ไม่ใส่ผัก"></textarea></label><div class="actions"><button class="primary" onclick="App.addConfiguredToCart('${menuId}')">เพิ่มลงตะกร้า</button><button class="secondary" onclick="App.closeModal()">ยกเลิก</button></div>`);
  }
  function addConfiguredToCart(menuId){
    const menu=state.menus.find(x=>x.id===menuId); const options={};
    Object.keys(menu.options||{}).forEach(k=>{const input=$("opt-"+k); if(input)options[k]=input.value}); const note=$("menu-note")?.value?.trim()||""; if(note)options.note=note;
    let unitPrice=Number(menu.price||0); Object.entries(options).forEach(([group,value])=>{const def=(menu.options?.[group]||[]).find(v=>String(typeof v==="object"?v.name:v)===String(value)); if(def&&typeof def==="object") unitPrice+=Number(def.price||0)});
    const key=menuId+"|"+JSON.stringify(options); const old=state.cart[key]; state.cart[key]={menu_id:menuId,quantity:(old?.quantity||0)+1,options,name:menu.name,unit_price:unitPrice}; persistCart(); renderCart(); closeModal(); toast("เพิ่มลงตะกร้าแล้ว");
  }
  function renderCart(){
    const rows=Object.entries(state.cart); let subtotal=0;
    $("cart-items").innerHTML=rows.map(([key,it])=>{subtotal+=Number(it.unit_price)*it.quantity;return `<div class="table-row"><div><b>${escapeHtml(it.name)}</b><div class="small">${Object.entries(it.options||{}).map(([k,v])=>`${escapeHtml(k)}: ${escapeHtml(v)}`).join(" · ")} · ฿${(Number(it.unit_price)*it.quantity).toFixed(2)}</div></div><div class="qty"><button onclick="App.cartAdd('${escapeAttr(key)}',-1)">−</button><b>${it.quantity}</b><button onclick="App.cartAdd('${escapeAttr(key)}',1)">+</button></div></div>`}).join("") || `<p class="small">ยังไม่มีรายการในตะกร้า</p>`;
    $("cart-subtotal").textContent=`฿${subtotal.toFixed(2)}`; $("cart-table").textContent=selectedTable()?.table_number||"ยังไม่ได้เลือกโต๊ะ";
  }
  function cartAdd(key,delta){if(!state.cart[key])return;state.cart[key].quantity+=delta;if(state.cart[key].quantity<=0)delete state.cart[key];persistCart();renderCart()}
  function clearCart(){state.cart={};persistCart();renderCart()}
  async function submitCart(){
    if(state.submittingCart)return;
    state.submittingCart=true;
    try{const live=await syncLiveCustomerTable();if(!live)return toast("ไม่พบโต๊ะที่คุณเข้าร่วม กรุณาเลือกโต๊ะใหม่"); const items=Object.values(state.cart).map(x=>({menu_id:x.menu_id,quantity:x.quantity,options:x.options||{}})); if(!items.length)return toast("กรุณาเพิ่มอาหารลงตะกร้า");
    await api("/api/customer/orders",{method:"POST",body:JSON.stringify({table_id:live.id,party_size:live.party_size,items,client_request_id:`${state.user.id}_${Date.now()}_${Math.random().toString(36).slice(2)}`})});clearCart();toast("ส่งออเดอร์เข้าครัวแล้ว");go("customer")}catch(e){toast(e.message)}finally{state.submittingCart=false}
  }
  async function loadCustomerTables(){
    try{const d=await api("/api/customer/tables");$("customer-tables").innerHTML=d.tables.map(t=>{const capacity=Number(t.capacity||4);const members=Number(t.occupant_count||0);const joinable=!!t.joinable;const available=t.status==="available";const disabled=!available&&!joinable;const info=available?"โต๊ะว่างพร้อมให้เลือก":joinable?`มีลูกค้าแล้ว ${members}/${Number(t.party_size)} คน · เหลือ ${Number(t.remaining_people)} ที่`:`โต๊ะเต็มแล้ว (${members}/${Number(t.party_size||capacity)} คน)`;return `<div class="table-card"><div class="top"><h3>โต๊ะ ${escapeHtml(t.table_number)}</h3><span class="badge ${escapeHtml(t.status)}">${escapeHtml(t.status)}</span></div><div class="info">${info}<br>ความจุสูงสุด ${capacity} คน</div><button class="primary full" ${disabled?"disabled":""} onclick="App.claimTable('${t.id}')">${available?"เลือกโต๊ะนี้":joinable?"เข้าร่วมโต๊ะ":"โต๊ะเต็ม"}</button></div>`}).join("")}catch(e){toast(e.message)}
  }
  async function claimTable(id){
    try{
      if(selectedTable() && selectedTable().id!==id){
        await api("/api/customer/leave",{method:"POST",body:JSON.stringify({reason:"change_table"})});
        state.selectedTable=null;state.tableVerified=false;stopPresence();state.cart={};localStorage.removeItem(tableKey());localStorage.removeItem(cartKey());state.hasActiveOrder=false;state.hadActiveOrder=false;
      }
      const d=await api("/api/customer/tables/claim",{method:"POST",body:JSON.stringify({table_id:id})});if(d.joined){state.selectedTable={...d.table,party_size:Number(d.table.party_size||0)};state.tableVerified=true;localStorage.setItem(tableKey(),JSON.stringify(state.selectedTable));startPresence();refreshNav();toast(`เข้าร่วมโต๊ะ ${d.table.table_number} แล้ว`);go("menu");return;}state.pendingTable=d.table;openModal(`<h2>ระบุจำนวนคนทั้งหมดในกลุ่ม</h2><p class="small">โต๊ะ ${escapeHtml(d.table.table_number)} รองรับสูงสุด ${Number(d.table.capacity||4)} คน</p><form onsubmit="App.confirmPartySize(event)"><label>จำนวนคนทั้งหมดในกลุ่ม<input id="party-size" type="number" min="1" max="${Number(d.table.capacity||4)}" required></label><div class="actions"><button class="primary">ยืนยันจำนวนคน</button><button type="button" class="secondary" onclick="App.cancelPartySize()">ยกเลิก</button></div></form>`)}
    catch(e){toast(e.message)}
  }
  async function cancelPartySize(){
    try{if(state.pendingTable) await api("/api/customer/tables/release",{method:"POST",body:JSON.stringify({table_id:state.pendingTable.id})})}catch(_){ }
    state.pendingTable=null;closeModal();loadCustomerTables();
  }
  async function confirmPartySize(event){
    event.preventDefault(); const table=state.pendingTable; const partySize=Number($("party-size")?.value||0);
    if(!table)return; if(!Number.isInteger(partySize)||partySize<1||partySize>Number(table.capacity||4))return toast(`จำนวนคนต้องอยู่ระหว่าง 1-${Number(table.capacity||4)} คน`);
    try{const d=await api("/api/customer/tables/confirm",{method:"POST",body:JSON.stringify({table_id:table.id,party_size:partySize})});state.pendingTable=null;state.selectedTable=d.table;state.tableVerified=true;localStorage.setItem(tableKey(),JSON.stringify(state.selectedTable));startPresence();closeModal();refreshNav();toast(`เลือกโต๊ะ ${d.table.table_number} สำหรับ ${partySize} คนแล้ว`);go("menu")}catch(e){toast(e.message)}
  }
  function reservationMinimumValue(){ const d=new Date(Date.now()+30*60*1000); const pad=n=>String(n).padStart(2,"0"); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }
  function setReservationMinimum(){ const input=$("res-date"); if(input){input.min=reservationMinimumValue(); input.title="ต้องจองล่วงหน้าอย่างน้อย 30 นาที";} }
  async function reserve(event) {
    event.preventDefault();
    if (!state.user) return go("login");
    if ($("res-date")?.value) { const [h,m]=$("res-date").value.split(":").map(Number); const selected=new Date(); selected.setHours(h,m,0,0); if(selected.getTime() < Date.now()+30*60*1000) return toast("กรุณาจองล่วงหน้าอย่างน้อย 30 นาที"); }
    try {
      const data = await api("/api/reservations",{method:"POST",body:JSON.stringify({
        customer_name:$("res-name").value, phone:$("res-phone").value,
        table_number:$("res-table").value, datetime:$("res-date").value
      })});
      toast("จองโต๊ะสำเร็จ"); event.target.reset(); loadReservations();
    } catch(e) { toast(e.message); }
  }

  const RES_LABEL = {waiting: "รอยืนยัน", confirmed: "ยืนยันแล้ว", seated: "นั่งแล้ว", cancelled: "ยกเลิก", expired: "หมดสิทธิ์"};
  function resLabel(k) { return RES_LABEL[k] || k; }
  function resBadge(status) { return `<span class="badge res-${escapeHtml(status)}">${escapeHtml(resLabel(status))}</span>`; }

  async function loadReservations() {
    if (!state.user || state.user.role !== "customer") return;
    try {
      const data = await api("/api/reservations");
      const today = new Date().toISOString().slice(0,10);
      const list = data.reservations.filter(r=>String(r.datetime||"").slice(0,10)===today).sort((a, b) => String(a.datetime).localeCompare(String(b.datetime)));
      $("my-reservations").innerHTML = `<h3>ปฏิทินการจองของฉันวันนี้</h3>` + (list.map(r=>`<div class="table-row"><div><b>${escapeHtml(String(r.datetime).slice(11,16))} · โต๊ะ ${escapeHtml(r.table_number)}</b><div class="small">${escapeHtml(r.customer_name)} · ${escapeHtml(r.phone||"")}</div></div>${resBadge(r.status)}</div>`).join("") || `<p class="small">วันนี้ยังไม่มีรายการจอง</p>`);
    } catch(e) { toast(e.message); }
  }

  const FOOD_STATUS = {pending:"รอทำอาหาร", cooking:"กำลังทำ", done:"เสร็จแล้ว"};
  function foodStatus(status){ return FOOD_STATUS[status] || status || "รอทำอาหาร"; }
  function renderCustomerOrders(orders){
    const html=orders.map(o=>{
      const items=(o.items||[]).map(it=>`<div class="table-row"><div><b>${escapeHtml(it.name)}</b><div class="small">จำนวน ${it.quantity} · ฿${(Number(it.unit_price)*Number(it.quantity)).toFixed(2)}</div></div><span class="badge">${escapeHtml(foodStatus(it.kitchen_status))}</span></div>`).join("");
      return `<div class="list-card" style="padding:16px;margin-top:12px"><div class="table-row"><b>โต๊ะ ${escapeHtml(o.table_number||"-")}</b><span class="badge">${escapeHtml(o.status)}</span></div>${items}<div class="small" style="margin-top:10px">ยอดรวม ฿${Number(o.total||0).toFixed(2)}</div></div>`;
    }).join("");
    return html || `<p class="small">ยังไม่มีออเดอร์</p>`;
  }
  async function loadCustomer(){
    if(!state.user)return; $("customer-name").textContent=state.user.name; $("customer-points").textContent=state.user.member_points||0; renderCart();
    try{
      const selected=selectedTable(); if(selected){const moves=await api("/api/customer/table-move-requests");const td=await api("/api/customer/tables");const completed=moves.requests.find(r=>r.status==="completed");if(completed){const moved=td.tables.find(t=>t.id===completed.new_table_id&&t.claimed_by===state.user.id);if(moved){state.selectedTable=moved;localStorage.setItem(tableKey(),JSON.stringify(moved));}}const live=td.tables.find(t=>t.id===state.selectedTable?.id);if(!live||live.status==="available"||!tableHasCurrentUser(live)){const had=state.hadActiveOrder;clearCustomerSessionData();state.hasActiveOrder=false;if(had){toast("บิลปิดแล้ว โต๊ะกลับมาว่าง");go("home");return;}return;}}
      const d=await api(`/api/orders?table_id=${encodeURIComponent(selectedTable()?.id || "")}`); const active=d.orders.filter(o=>!['closed','merged','cancelled'].includes(o.status)); state.hadActiveOrder=state.hasActiveOrder||active.length>0; state.hasActiveOrder=active.length>0; refreshNav();
      $("customer-orders").innerHTML=`<h3>รายการอาหารที่สั่ง</h3>`+renderCustomerOrders(d.orders);
      if(active.length) $("customer-orders").insertAdjacentHTML("beforeend",`<div class="actions"><button class="primary" onclick="App.go('menu')">สั่งเมนูใหม่</button><button class="secondary" onclick="App.requestSplitBill()">ขอแยกบิล</button></div>`);
    }catch(e){ $("customer-orders").innerHTML=`<p class="small">${escapeHtml(e.message)}</p>`; }
  }
  async function requestMoveTable(){
    try{const d=await api("/api/customer/tables");const current=selectedTable()?.id;const free=d.tables.filter(t=>t.status==="available"&&t.id!==current);if(!free.length)return toast("ไม่มีโต๊ะว่างให้ย้าย");openModal(`<h2>ขอย้ายโต๊ะ</h2><p class="small">เลือกโต๊ะปลายทาง แล้วรอ Staff ยืนยัน</p><form onsubmit="App.submitMoveRequest(event)"><label>โต๊ะปลายทาง<select id="move-target">${free.map(t=>`<option value="${escapeAttr(t.id)}">โต๊ะ ${escapeHtml(t.table_number)}</option>`).join("")}</select></label><div class="actions"><button class="primary">ส่งคำขอ</button><button type="button" class="secondary" onclick="App.closeModal()">ยกเลิก</button></div></form>`)}catch(e){toast(e.message)}
  }
  async function submitMoveRequest(e){e.preventDefault();try{const d=await api("/api/customer/table-move-requests",{method:"POST",body:JSON.stringify({new_table_id:$('move-target').value})});closeModal();toast(d.message||"ส่งคำขอย้ายโต๊ะให้ Staff แล้ว")}catch(e){toast(e.message)}}
  async function requestSplitBill(){
    try{
      const live=await syncLiveCustomerTable(); if(!live)return toast("กรุณาเลือกโต๊ะก่อนขอแยกบิล");
      const d=await api("/api/customer/split-bill-requests",{method:"POST",body:JSON.stringify({table_id:live.id})});
      const members=d.members||[]; const profiles=d.member_profiles||{}; const names={}; members.forEach(id=>{names[id]=id===state.user.id?"ฉัน":(profiles[id]?.name||id)});
      const orders=(d.orders||[]).filter(o=>!['closed','merged','cancelled'].includes(o.status));
      const rows=orders.map(order=>`<div class="list-card" style="margin:8px 0"><b>${order.bill_group==='split'?'บิลย่อย':'บิลกลาง'} · ${escapeHtml(order.id)}</b>${(order.items||[]).map((it,i)=>`<label class="table-row" style="gap:10px"><span style="flex:1">${escapeHtml(it.name||'เมนู')} × ${Number(it.quantity||0)}<small class="small" style="display:block">฿${(Number(it.unit_price||0)*Number(it.quantity||0)).toFixed(2)}</small></span><select class="split-owner" data-order="${escapeAttr(order.id)}" data-index="${i}">${members.map(id=>`<option value="${escapeAttr(id)}" ${String(it.customer_id||d.default_owner)===String(id)?'selected':''}>${escapeHtml(names[id])}</option>`).join('')}</select></label>`).join('')}</div>`).join('');
      openModal(`<h2>เลือกเมนูสำหรับแต่ละบิล</h2><p class="small">ยังคงเป็นโต๊ะเดียวกัน แต่จะแยกเป็นหลายบิล เจ้าของเริ่มต้นคือคนแรกของโต๊ะ และสามารถเปลี่ยนได้ภายหลัง</p><div>${rows||'<p>ไม่มีรายการอาหาร</p>'}</div><div class="actions"><button class="primary" onclick="App.saveSplitOwners('${escapeAttr(selectedTable()?.id||'')}')">บันทึกการแยกบิล</button><button class="secondary" onclick="App.closeModal()">ยกเลิก</button></div>`);
    } catch(e){toast(e.message)}
  }
  async function saveSplitOwners(tableId){
    try { const assignments=[...document.querySelectorAll('.split-owner')].map(x=>({order_id:x.dataset.order,item_index:Number(x.dataset.index),customer_id:x.value})); const d=await api('/api/customer/split-bill-requests',{method:'POST',body:JSON.stringify({table_id:tableId,assignments})}); closeModal(); toast(d.message||'บันทึกการแยกบิลแล้ว'); loadCustomer(); } catch(e){toast(e.message)}
  }
  async function requestBill(){
    const live=await syncLiveCustomerTable(); if(!live)return toast("กรุณาเลือกโต๊ะก่อนเช็คบิล");
    const all=(await api(`/api/orders?table_id=${encodeURIComponent(live.id)}`)).orders.filter(x=>!['closed','merged','cancelled'].includes(x.status)); const o=all.find(x=>x.customer_id===state.user.id)||all.find(x=>x.bill_group==='central')||all[0]; if(!o)return toast("ยังไม่มีบิลที่เปิดอยู่");
    try{
      const b=await api(`/api/orders/${o.id}/bill?discount=0`);
      const points=Number(b.customer_points ?? state.user?.member_points ?? 0); const maxUsable=Math.floor(points/100)*100;
      const pointChoice=maxUsable?`<label>ใช้แต้มสะสม (100 แต้ม ลด 10 บาท)<input id="customer-points-use" type="number" min="0" max="${maxUsable}" step="100" value="0"><span class="small">ใช้ได้สูงสุด ${maxUsable} แต้ม</span></label>`:`<p class="small">แต้มสะสมปัจจุบัน ${points} แต้ม — ต้องมีอย่างน้อย 100 แต้มจึงใช้เป็นส่วนลดได้</p>`;
      const checkoutAction=o.status==="waiting_bill"?`<button class="primary" disabled>พนักงานกำลังมา</button>`:`<button class="primary" onclick="App.callStaff('${o.id}')">เรียกพนักงานเช็คบิล</button>`;
      const waitingNote=o.status==="waiting_bill"?`<p class="small">พนักงานกำลังมา กรุณารอสักครู่ ไม่ต้องกดเรียกซ้ำ</p>`:`<p class="small">กดแล้วระบบจะแจ้งพนักงานให้มาเช็คบิล และแต้มจะถูกหักเมื่อปิดบิลสำเร็จ</p>`;
      openModal(`<h2>ยอดบิล โต๊ะ ${escapeHtml(o.table_number||"")}</h2>${billRows(b.bill)}${pointChoice}${waitingNote}<div class="actions">${checkoutAction}<button class="secondary" onclick="App.closeModal()">ปิด</button></div>`)
    }catch(e){toast(e.message)}
  }
  async function callStaff(id){try{const points=Number($("customer-points-use")?.value||0);await api(`/api/orders/${id}/request-checkout`,{method:"PATCH",body:JSON.stringify({points_to_use:points})});closeModal();toast("เรียกพนักงานเช็คบิลแล้ว");loadCustomer()}catch(e){toast(e.message)}}
  function summaryRows(rows, total) {
    return rows.map(([label, n]) => `<div style="margin:12px 0"><div class="table-row" style="border:0;padding:0"><span>${escapeHtml(label)}</span><b>${n}</b></div><div class="meter"><i style="width:${total ? Math.round(n / total * 100) : 0}%"></i></div></div>`).join("") || `<p class="small">ยังไม่มีข้อมูล</p>`;
  }

  async function loadDashboard() {
    try {
      const d = await api("/api/dashboard");
      const baht = n => `฿${Number(n || 0).toLocaleString("th-TH", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
      $("stat-today").textContent = baht(d.today_sales);
      $("stat-total").textContent = baht(d.total_sales);
      $("stat-orders").textContent = d.closed_orders;
      $("stat-avg").textContent = baht(d.avg_bill);
      $("stat-open").textContent = d.open_orders;
      $("stat-res-today").textContent = d.reservations.today;
      $("stat-res-total").textContent = d.reservations.total;
      $("stat-users").textContent = d.users.total;
      $("best-sellers").innerHTML = d.best_sellers.map((x,i)=>`<div class="table-row"><span>${i+1}. ${escapeHtml(x.name)}</span><b>${x.quantity}</b></div>`).join("") || `<p class="small">ยังไม่มีข้อมูลยอดขาย</p>`;
      const max = Math.max(...d.sales_7d.map(x => x.total), 1);
      $("sales-chart").innerHTML = d.sales_7d.map(x => `<div class="bar-col"><span class="bar-val">${x.total ? Math.round(x.total) : ""}</span><div class="bar" style="height:${Math.round(x.total / max * 100)}%"></div><span class="bar-label">${escapeHtml(x.date.slice(5))}</span></div>`).join("");
      const t = d.tables;
      $("table-summary").innerHTML = summaryRows([["ว่าง", t.available], ["มีลูกค้า", t.occupied], ["รอเช็คบิล", t.waiting_bill], ["จองแล้ว", t.reserved]], t.total);
      const rs = d.reservations.by_status;
      $("res-summary").innerHTML = summaryRows(Object.entries(rs).map(([k, v]) => [resLabel(k), v]), d.reservations.total);
      const u = d.users;
      $("user-summary").innerHTML = summaryRows([["Admin", u.admin], ["Staff", u.staff], ["Customer", u.customer]], u.total) + (u.inactive ? `<p class="small">ถูกปิดการใช้งาน ${u.inactive} บัญชี</p>` : "");
    } catch(e) { toast(e.message); }
  }

  // ---------- Staff: reservations (shared with the customer reservation system) ----------
  const staffState = { tables: [] };

  function renderStaffReservations(list) {
    const rows = list.slice().sort((a, b) => String(a.datetime).localeCompare(String(b.datetime)));
    $("staff-reservations").innerHTML = rows.map(x => {
      const acts = [];
      if (x.status === "waiting") acts.push(`<button class="secondary" onclick="App.resAction('${x.id}','confirmed')">ยืนยัน</button>`);
      if (x.status === "confirmed") acts.push(`<button class="primary" onclick="App.resAction('${x.id}','seated')">จัดเข้านั่ง</button>`);
      if (x.status === "waiting" || x.status === "confirmed") acts.push(`<button class="secondary" onclick="App.resAction('${x.id}','cancelled')">ยกเลิก</button>`);
      return `<div class="table-row" style="align-items:flex-start"><div><b>โต๊ะ ${escapeHtml(x.table_number)}</b><div class="small">${escapeHtml(String(x.datetime).replace("T", " "))} · ${escapeHtml(x.customer_name)} · ${escapeHtml(x.phone || "-")}${x.source === "staff" ? " · จองโดยพนักงาน" : ""}</div><div class="row-actions">${acts.join("")}</div></div>${resBadge(x.status)}</div>`;
    }).join("") || `<p class="small">ไม่มีคิว</p>`;
  }

  async function resAction(id, status) {
    if (status === "cancelled" && !confirm("ยกเลิกการจองนี้หรือไม่?")) return;
    try { await api(`/api/reservations/${id}`, {method: "PATCH", body: JSON.stringify({status})}); toast("อัปเดตการจองแล้ว"); loadStaff(); }
    catch(e) { toast(e.message); }
  }

  async function showStaffReserve() {
    try {
      if (!staffState.tables.length) staffState.tables = (await api("/api/tables")).tables;
    } catch(e) { return toast(e.message); }
    const tables = staffState.tables.slice().sort((a, b) => String(a.table_number).localeCompare(String(b.table_number), undefined, {numeric: true}));
    openModal(`<h2>จองโต๊ะให้ลูกค้า</h2><form onsubmit="App.saveStaffReserve(event)"><label>ชื่อลูกค้า<input id="sr-name" required></label><label>เบอร์โทร<input id="sr-phone" type="tel" inputmode="numeric" pattern="[0-9]{10}" maxlength="10" minlength="10" required></label><label>จำนวนคน<input id="sr-party-size" type="number" min="1" required></label><label>โต๊ะ<select id="sr-table">${tables.map(t => `<option value="${escapeAttr(t.table_number)}">โต๊ะ ${escapeHtml(t.table_number)}</option>`).join("")}</select></label><label>เวลา<input id="sr-date" type="time" required></label><div class="actions"><button class="primary">บันทึกการจอง</button><button type="button" class="secondary" onclick="App.closeModal()">Cancel</button></div></form>`);
  }

  async function saveStaffReserve(e) {
    e.preventDefault();
    try {
      await api("/api/reservations", {method: "POST", body: JSON.stringify({customer_name: $("sr-name").value, phone: $("sr-phone").value, party_size: Number($("sr-party-size").value), table_number: $("sr-table").value, datetime: $("sr-date").value})});
      closeModal(); toast("บันทึกการจองสำเร็จ"); loadStaff();
    } catch(x) { toast(x.message); }
  }

  // ---------- Staff: Counter order (order on behalf of the customer) ----------
  const pos = { menus: [], cart: {}, tables: [] };
  async function loadPos(){
    try{const [m,t]=await Promise.all([api("/api/menus?page=1&page_size=100"),api("/api/tables")]);pos.menus=m.items;pos.tables=t.tables.slice().sort((a,b)=>String(a.table_number).localeCompare(String(b.table_number),undefined,{numeric:true}));const sel=$("pos-table"),keep=sel.value;sel.innerHTML=pos.tables.map(x=>`<option value="${escapeAttr(x.id)}" ${x.status==="reserved"?"disabled":""}>โต๊ะ ${escapeHtml(x.table_number)} (${escapeHtml(x.status)})</option>`).join("");if(keep&&pos.tables.some(x=>x.id===keep&&x.status!=="reserved"))sel.value=keep;renderPos()}catch(e){toast(e.message)}
  }
  function renderPos(){
    const q=$("pos-search").value.trim().toLowerCase();const category=$("pos-category")?.value||"";$("pos-menu").innerHTML=pos.menus.filter(m=>(!q||String(m.name).toLowerCase().includes(q)||String(m.category).toLowerCase().includes(q))&&(!category||String(m.category)===category)).map(m=>`<div class="pos-item"><div><b>${escapeHtml(m.name)}</b><span class="small">${escapeHtml(m.category)} · ฿${Number(m.price).toFixed(2)}${m.is_out_of_stock?" · หมด":""}</span></div><button class="secondary" ${m.is_out_of_stock?"disabled":""} onclick="App.posChoose('${m.id}')">+</button></div>`).join("")||`<p class="small">ไม่พบเมนู</p>`;
    const ids=Object.keys(pos.cart);let total=0;$("pos-cart").innerHTML=ids.map(key=>{const it=pos.cart[key];total+=Number(it.unit_price)*it.quantity;return `<div class="table-row"><div><b>${escapeHtml(it.name)}</b><div class="small">${Object.entries(it.options||{}).map(([k,v])=>`${escapeHtml(k)}: ${escapeHtml(v)}`).join(" · ")} · ฿${(Number(it.unit_price)*it.quantity).toFixed(2)}</div></div><div class="qty"><button onclick="App.posAdd('${escapeAttr(key)}',-1)">−</button><b>${it.quantity}</b><button onclick="App.posAdd('${escapeAttr(key)}',1)">+</button></div></div>`}).join("")||`<p class="small">ยังไม่ได้เลือกเมนู</p>`;$("pos-total").textContent=`฿${total.toFixed(2)}`;
  }
  function posChoose(menuId){const menu=pos.menus.find(x=>x.id===menuId);if(!menu)return;const fields=Object.entries(menu.options||{}).map(([key,values])=>`<label>${escapeHtml(key)}<select id="pos-opt-${escapeAttr(key)}">${(Array.isArray(values)?values:[]).map(v=>{const name=typeof v==="object"?v.name:v,price=typeof v==="object"?Number(v.price||0):0;return `<option value="${escapeAttr(name)}">${escapeHtml(name)}${price?` (+฿${price.toFixed(2)})`:""}</option>`}).join("")}</select></label>`).join("");openModal(`<h2>${escapeHtml(menu.name)}</h2>${fields}<label>หมายเหตุเพิ่มเติม<span style="display:block"></span><textarea id="pos-note" rows="3" style="display:block;width:100%;margin-top:7px"></textarea></label><div class="actions"><button class="primary" onclick="App.posAddConfigured('${menuId}')">เพิ่มรายการ</button><button class="secondary" onclick="App.closeModal()">ยกเลิก</button></div>`)}
  function posAddConfigured(menuId){const menu=pos.menus.find(x=>x.id===menuId),options={};Object.keys(menu.options||{}).forEach(k=>{const input=$("pos-opt-"+k);if(input)options[k]=input.value});const note=$("pos-note")?.value.trim();if(note)options.note=note;let unitPrice=Number(menu.price||0);Object.entries(options).forEach(([g,v])=>{const d=(menu.options?.[g]||[]).find(x=>String(typeof x==="object"?x.name:x)===String(v));if(d&&typeof d==="object")unitPrice+=Number(d.price||0)});const key=menuId+"|"+JSON.stringify(options),old=pos.cart[key];pos.cart[key]={menu_id:menuId,quantity:(old?.quantity||0)+1,options,name:menu.name,unit_price:unitPrice};closeModal();renderPos()}
  function posAdd(key,delta){if(!pos.cart[key])return;pos.cart[key].quantity+=delta;if(pos.cart[key].quantity<=0)delete pos.cart[key];renderPos()}
  function posClear(){pos.cart={};renderPos()}
  async function posSubmit(){const items=Object.values(pos.cart).map(x=>({menu_id:x.menu_id,quantity:x.quantity,options:x.options||{}}));if(!items.length)return toast("กรุณาเลือกเมนูก่อน");const table_id=$("pos-table").value;if(!table_id)return toast("กรุณาเลือกโต๊ะ");try{await api("/api/orders",{method:"POST",body:JSON.stringify({table_id,items})});toast("ส่งออเดอร์เข้าครัวแล้ว");pos.cart={};loadPos()}catch(e){toast(e.message)}}
  // ---------- Staff: checkout & receipt ----------
  let billTimer = null;

  function billRows(b) {
    const r = (l, v, bold) => `<div class="table-row" style="padding:7px 0;${bold ? "font-size:17px" : ""}"><span>${l}</span><${bold ? "b" : "span"}>฿${Number(v).toFixed(2)}</${bold ? "b" : "span"}></div>`;
    const manual = b.manual_discount ?? b.discount ?? 0;
    const pointRow = b.points_discount ? r(`ส่วนลดจากแต้ม (${Number(b.points_used || 0)} แต้ม)`, -b.points_discount) : "";
    return r("ยอดรวม", b.subtotal) + (manual ? r("ส่วนลด", -manual) : "") + pointRow + r("Service charge", b.service_charge) + r("VAT", b.tax) + r("ยอดสุทธิ", b.total, true);
  }

  async function showCheckout(orderId) {
    try {
      const d = await api(`/api/orders/${orderId}/bill?discount=0`);
      const o = d.order;
      const rows = (o.items || []).map(it => `<div class="table-row" style="padding:7px 0"><span>${escapeHtml(it.name)} × ${it.quantity}</span><span>฿${(Number(it.unit_price) * Number(it.quantity)).toFixed(2)}</span></div>`).join("");
      openModal(`<h2>เช็คบิล โต๊ะ ${escapeHtml(o.table_number || "-")}</h2>${rows}<label>ส่วนลด (บาท)<input id="co-discount" type="number" min="0" step="0.01" value="0" oninput="App.previewBill('${orderId}')"></label><div id="co-summary">${billRows(d.bill)}</div><div class="actions"><button class="primary" onclick="App.doCheckout('${orderId}')">ยืนยันเช็คบิล</button><button type="button" class="secondary" onclick="App.closeModal()">Cancel</button></div>`);
    } catch(e) { toast(e.message); }
  }

  function previewBill(orderId) {
    clearTimeout(billTimer);
    billTimer = setTimeout(async () => {
      try {
        const d = await api(`/api/orders/${orderId}/bill?discount=${encodeURIComponent($("co-discount").value || 0)}`);
        $("co-summary").innerHTML = billRows(d.bill);
      } catch(e) { $("co-summary").innerHTML = `<p class="small">${escapeHtml(e.message)}</p>`; }
    }, 250);
  }

  async function doCheckout(orderId) {
    try {
      const d = await api(`/api/orders/${orderId}/checkout`, {method: "PATCH", body: JSON.stringify({discount: $("co-discount").value || 0})});
      toast("เช็คบิลสำเร็จ");
      showReceipt(d.order);
      if ($("tables").classList.contains("active")) loadTables(); else loadStaff();
    } catch(e) { toast(e.message); }
  }

  function showReceipt(o) {
    const rows = (o.items || []).map(it => `<div class="table-row" style="padding:5px 0;border:0"><span>${escapeHtml(it.name)} × ${it.quantity}</span><span>฿${(Number(it.unit_price) * Number(it.quantity)).toFixed(2)}</span></div>`).join("");
    const when = o.closed_at ? new Date(o.closed_at).toLocaleString("th-TH") : "";
    openModal(`<div class="receipt"><h2 style="text-align:center;margin-bottom:0">itailaew</h2><div class="small" style="text-align:center">Italian Restaurant & Café</div><div class="small" style="text-align:center;margin:8px 0 14px">ใบเสร็จรับเงิน · โต๊ะ ${escapeHtml(o.table_number || "-")} · ${escapeHtml(when)}</div>${rows}<hr style="border:0;border-top:1px dashed var(--line)">${billRows(o)}<div class="small" style="text-align:center;margin-top:12px">ได้รับแต้ม ${Number(o.earned_points || 0)} แต้ม · แต้มที่ใช้ ${Number(o.points_used || 0)} แต้ม<br>ขอบคุณที่ใช้บริการ</div></div><div class="actions no-print"><button class="primary" onclick="window.print()">พิมพ์ใบเสร็จ</button><button class="secondary" onclick="App.closeModal()">ปิด</button></div>`);
  }

  // ---------- Staff: Tables page ----------
  const tableState = { tables: [], orders: [], reservations: [] };

  async function loadTables() {
    try {
      const [t, o, r] = await Promise.all([api("/api/tables"), api("/api/orders"), api("/api/reservations")]);
      tableState.tables = t.tables.slice().sort((a, b) => String(a.table_number).localeCompare(String(b.table_number), undefined, {numeric: true}));
      tableState.orders = o.orders; tableState.reservations = r.reservations;
      renderTables();
    } catch(e) { toast(e.message); }
  }

  function renderTables() {
    const { tables, orders, reservations } = tableState;
    const count = s => tables.filter(x => x.status === s).length;
    $("tables-summary").innerHTML = [["ทั้งหมด", tables.length], ["ว่าง", count("available")], ["มีลูกค้า", count("occupied")], ["รอเช็คบิล", count("waiting_bill")]]
      .map(([l, n]) => `<div><span>${l}</span><b>${n}</b></div>`).join("");
    $("tables-grid").innerHTML = tables.map(tb => {
      const order = tb.current_order_id ? orders.find(o => o.id === tb.current_order_id) : null;
      const resv = reservations.filter(r => String(r.table_number) === String(tb.table_number) && ["waiting","confirmed"].includes(r.status));
      const opts = order ? ["occupied", "waiting_bill"] : ["available", "occupied", "reserved"];
      const select = `<select onchange="App.setTableStatus('${tb.id}',this.value)">${opts.map(v => `<option value="${v}" ${tb.status === v ? "selected" : ""}>${v}</option>`).join("")}</select>`;
      const info = order
        ? `${(order.items || []).length} รายการ · รวม <b>฿${Number(order.total || 0).toFixed(2)}</b><br>เปิดเมื่อ ${escapeHtml((order.created_at || "").slice(0, 16).replace("T", " "))}`
        : `ไม่มีออเดอร์`;
      const resInfo = resv.length ? `<br>📅 จอง ${resv.length} คิว (${escapeHtml(resv.map(r => r.customer_name + " " + String(r.datetime).slice(11, 16)).join(", "))})` : "";
      const buttons = order ? `<button class="secondary" onclick="App.showMove('${tb.id}')">ย้ายโต๊ะ</button><button class="secondary" onclick="App.showMerge('${tb.id}')">รวมโต๊ะ</button>${order.split_requested?`<button class="secondary" onclick="App.splitByCustomer('${order.id}')">แยกบิลตาม Customer</button>`:""}<button class="primary" onclick="App.showCheckout('${order.id}')">เช็คบิล</button>` : "";
      const capacityEditor = state.user?.role === "admin" ? `<label class="small">รับได้สูงสุด<input type="number" min="1" max="100" value="${Number(tb.capacity||4)}" onchange="App.setTableCapacity('${tb.id}',this.value)"></label>` : `<div class="small">รองรับสูงสุด ${Number(tb.capacity||4)} คน</div>`;
      return `<div class="table-card"><div class="top"><h3>โต๊ะ ${escapeHtml(tb.table_number)}</h3><span class="badge ${escapeHtml(tb.status)}">${escapeHtml(tb.status)}</span></div><div class="info">${info}${resInfo}</div>${capacityEditor}${select}<div class="actions">${buttons}</div></div>`;
    }).join("") || `<div class="list-card">ยังไม่มีโต๊ะ</div>`;
  }

  async function setTableStatus(id, status) {
    try { await api(`/api/tables/${id}`, {method: "PATCH", body: JSON.stringify({status})}); toast("อัปเดตสถานะโต๊ะแล้ว"); loadTables(); }
    catch(e) { toast(e.message); loadTables(); }
  }
  async function setTableCapacity(id, capacity) {
    try { await api(`/api/tables/${id}`, {method:"PATCH",body:JSON.stringify({capacity:Number(capacity)})}); toast("บันทึกความจุโต๊ะแล้ว"); loadTables(); }
    catch(e) { toast(e.message); loadTables(); }
  }

  function openModal(html) { $("modal").classList.remove("hidden"); $("modal").innerHTML = `<div>${html}</div>`; }
  function tableOptions(list) { return list.map(t => `<option value="${escapeAttr(t.id)}">โต๊ะ ${escapeHtml(t.table_number)}</option>`).join(""); }
  function tableById(id) { return tableState.tables.find(t => t.id === id); }

  function showMove(id) {
    const free = tableState.tables.filter(t => t.status === "available");
    if (!free.length) return toast("ไม่มีโต๊ะว่างให้ย้าย");
    openModal(`<h2>ย้ายโต๊ะ ${escapeHtml(tableById(id).table_number)}</h2><form onsubmit="App.doMove(event,'${id}')"><label>ย้ายไปโต๊ะ<select id="mv-to">${tableOptions(free)}</select></label><div class="actions"><button class="primary">ย้ายโต๊ะ</button><button type="button" class="secondary" onclick="App.closeModal()">Cancel</button></div></form>`);
  }
  async function doMove(e, id) {
    e.preventDefault();
    try { await api("/api/tables/move", {method: "POST", body: JSON.stringify({old_table_id: id, new_table_id: $("mv-to").value})}); closeModal(); toast("ย้ายโต๊ะสำเร็จ"); loadTables(); }
    catch(x) { toast(x.message); }
  }

  function showMerge(id) {
    const others = tableState.tables.filter(t => t.id !== id && t.current_order_id);
    if (!others.length) return toast("ไม่มีโต๊ะอื่นที่มีออเดอร์ให้รวม");
    openModal(`<h2>รวมโต๊ะ ${escapeHtml(tableById(id).table_number)} เข้ากับ</h2><form onsubmit="App.doMerge(event,'${id}')"><label>โต๊ะปลายทาง (บิลจะไปรวมที่โต๊ะนี้)<select id="mg-to">${tableOptions(others)}</select></label><div class="actions"><button class="primary">รวมโต๊ะ</button><button type="button" class="secondary" onclick="App.closeModal()">Cancel</button></div></form>`);
  }
  async function doMerge(e, id) {
    e.preventDefault();
    try { await api("/api/tables/merge", {method: "POST", body: JSON.stringify({source_table_id: id, target_table_id: $("mg-to").value})}); closeModal(); toast("รวมโต๊ะสำเร็จ"); loadTables(); }
    catch(x) { toast(x.message); }
  }

  function showSplit(id) {
    const tb = tableById(id);
    const order = tableState.orders.find(o => o.id === tb.current_order_id);
    const free = tableState.tables.filter(t => t.status === "available");
    if (!order || !(order.items || []).length) return toast("ไม่พบรายการในออเดอร์");
    if (!free.length) return toast("ไม่มีโต๊ะว่างสำหรับบิลที่แยก");
    const rows = order.items.map((it, i) => `<label style="display:flex;gap:10px;align-items:center;margin:8px 0"><input type="checkbox" class="sp-item" value="${i}" style="width:auto;margin:0"> ${escapeHtml(it.name)} × ${it.quantity} <span class="small">฿${(Number(it.unit_price) * Number(it.quantity)).toFixed(2)}</span></label>`).join("");
    openModal(`<h2>แยกบิล โต๊ะ ${escapeHtml(tb.table_number)}</h2><form onsubmit="App.doSplit(event,'${order.id}')">${rows}<label>ย้ายรายการที่เลือกไปโต๊ะ<select id="sp-to">${tableOptions(free)}</select></label><div class="actions"><button class="primary">แยกบิล</button><button type="button" class="secondary" onclick="App.closeModal()">Cancel</button></div></form>`);
  }
  async function doSplit(e, orderId) {
    e.preventDefault();
    const picked = [...document.querySelectorAll(".sp-item:checked")].map(x => Number(x.value));
    if (!picked.length) return toast("กรุณาเลือกรายการที่ต้องการแยก");
    const order = tableState.orders.find(o => o.id === orderId);
    if (order && picked.length >= (order.items || []).length) return toast("เลือกได้ไม่เกินจำนวนรายการ เพื่อให้เหลือรายการอย่างน้อย 1 อย่างในบิลเดิม");
    try { await api("/api/orders/split", {method: "POST", body: JSON.stringify({order_id: orderId, item_indexes: picked, new_table_id: $("sp-to").value})}); closeModal(); toast("แยกบิลสำเร็จ"); loadTables(); }
    catch(x) { toast(x.message); }
  }

  function renderMoveRequests(list){
    const el=$("staff-move-requests"); if(!el)return;
    el.innerHTML=list.map(x=>`<div class="table-row"><div><b>โต๊ะ ${escapeHtml(x.table_number||"-")}</b><div class="small">${escapeHtml(x.customer_name||"ลูกค้า")} · ไปโต๊ะ ${escapeHtml(x.new_table_number||"-")} · ${escapeHtml(x.created_at||"")}</div></div><div class="row-actions"><span class="badge">${escapeHtml(x.status)}</span>${x.status==="pending"?`<button class="secondary" onclick="App.updateMoveRequest('${x.id}','acknowledged')">รับทราบ</button>`:""}${x.status!=="completed"&&x.status!=="cancelled"?`<button class="primary" onclick="App.updateMoveRequest('${x.id}','completed')">ยืนยันย้ายจริง</button>`:""}</div></div>`).join("")||`<p class="small">ไม่มีคำขอย้ายโต๊ะ</p>`;
  }
  async function updateMoveRequest(id,status){try{await api(`/api/table-move-requests/${id}`,{method:"PATCH",body:JSON.stringify({status})});toast("อัปเดตคำขอย้ายโต๊ะแล้ว");loadStaff()}catch(e){toast(e.message)}}
  async function loadStaff() {
    try {
      const [t,k,r,o,m,n] = await Promise.all([api("/api/tables"),api("/api/kitchen"),api("/api/reservations"),api("/api/orders"),api("/api/table-move-requests"),api("/api/notifications")]);
      $("tables-list").innerHTML = t.tables.map(x=>`<div class="table-row"><div><b>โต๊ะ ${escapeHtml(x.table_number)}</b></div><span class="badge ${x.status}">${escapeHtml(x.status)}</span></div>`).join("");
      const kitchenItems = k.items.slice().sort((a,b) => {
        const rank = x => x.status === "done" ? 1 : 0;
        const rankDiff = rank(a) - rank(b);
        if (rankDiff) return rankDiff;
        const timeA = a.status === "done" ? (a.updated_at || a.timestamp || "") : (a.timestamp || "");
        const timeB = b.status === "done" ? (b.updated_at || b.timestamp || "") : (b.timestamp || "");
        return String(timeA).localeCompare(String(timeB));
      });
      const groups = kitchenItems.reduce((acc,x)=>{(acc[x.order_id] ||= []).push(x);return acc;},{});
      $("kitchen-list").innerHTML = Object.entries(groups).map(([orderId,items])=>`<section class="kitchen-bill"><div class="kitchen-bill-head"><b>บิล ${escapeHtml(orderId)} · โต๊ะ ${escapeHtml(items[0].table_number||"-")}</b><span class="small">${escapeHtml(items[0].timestamp||"")}</span></div>${items.map(x=>`<div class="table-row"><div><b>${escapeHtml(x.menu_name)}</b><div class="small">× ${x.quantity}</div></div><select onchange="App.updateKitchen('${x.id}',this.value)"><option ${x.status==="pending"?"selected":""}>pending</option><option ${x.status==="cooking"?"selected":""}>cooking</option><option ${x.status==="done"?"selected":""}>done</option></select></div>`).join("")}</section>`).join("") || `<p class="small">ยังไม่มีออเดอร์เข้าครัว</p>`;
      $("staff-notifications").innerHTML = `<h3>การแจ้งเตือน</h3>` + ((n.notifications||[]).map(x=>`<div class="table-row"><div><b>${escapeHtml(x.title||"")}</b><div class="small">${escapeHtml(x.detail||"")}</div></div><span class="small">${escapeHtml(String(x.created_at||"").slice(11,19))}</span></div>`).join("") || `<p class="small">ไม่มีการแจ้งเตือน</p>`);
      staffState.tables = t.tables; renderStaffReservations(r.reservations); renderMoveRequests(m.requests);
      $("staff-orders").innerHTML = o.orders.slice().sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))).map(x => {
        const open = !["closed", "merged", "cancelled"].includes(x.status);
        return `<div class="table-row"><div><b>โต๊ะ ${escapeHtml(x.table_number||"-")}</b><div class="small">Total ฿${Number(x.total||0).toFixed(2)}</div>${open ? `<div class="row-actions"><button class="primary" onclick="App.showCheckout('${x.id}')">เช็คบิล</button>${x.split_requested?`<button class="secondary" onclick="App.splitByCustomer('${x.id}')">แยกบิลตาม Customer</button>`:""}</div>` : ""}</div><span class="badge">${escapeHtml(x.status)}</span></div>`;
      }).join("") || `<p class="small">ไม่มีออเดอร์</p>`;
    } catch(e) { toast(e.message); }
  }

  async function splitByCustomer(orderId){try{await api("/api/orders/split-by-customer",{method:"POST",body:JSON.stringify({order_id:orderId})});toast("แยกบิลตาม Customer สำเร็จ รายการ Counter อยู่บิลกลาง");loadStaff()}catch(e){toast(e.message)}}
  async function updateKitchen(id,status) {
    try { await api(`/api/kitchen/${id}`,{method:"PATCH",body:JSON.stringify({status})}); toast("อัปเดตครัวแล้ว"); }
    catch(e){toast(e.message)}
  }

  async function adminPage(type) {
    const c=$("admin-content");
    if(type==="menus") {
      try {
        const data=await api("/api/menus?page=1&page_size=100");
        c.innerHTML=`<div class="list-card"><div class="section-head" style="margin:0 0 15px"><h3>Menu Management</h3><button class="primary" onclick="App.showMenuForm()">+ Add Menu</button></div><table><tr><th>Menu</th><th>Category</th><th>Price</th><th>Status</th><th></th></tr>${data.items.map(m=>`<tr><td>${escapeHtml(m.name)}</td><td>${escapeHtml(m.category)}</td><td>฿${Number(m.price).toFixed(2)}</td><td>${m.is_out_of_stock?"หมด":"พร้อมขาย"}</td><td><button class="secondary" onclick='App.showMenuForm(${JSON.stringify(m)})'>Edit</button><button class="secondary" onclick="App.deleteMenu('${m.id}')">Delete</button></td></tr>`).join("")}</table></div>`;
      }catch(e){toast(e.message)}
    }
    if(type==="users") {
      try {
        const d=await api("/api/users");
        c.innerHTML=`<div class="list-card"><div class="section-head" style="margin:0 0 15px"><h3>User Management</h3><button class="primary" onclick="App.showStaffForm()">+ Add Staff</button></div><table><tr><th>Name</th><th>Email</th><th>Role</th><th>Active</th><th></th></tr>${d.users.map(u=>`<tr><td>${escapeHtml(u.name)}</td><td>${escapeHtml(u.email)}</td><td>${escapeHtml(u.role)}</td><td>${u.active?"Yes":"No"}</td><td>${u.role!=="admin"?`<button class="secondary" onclick="App.toggleUser('${u.id}',${!u.active})">${u.active?"Disable":"Enable"}</button>`:""}</td></tr>`).join("")}</table></div>`;
      }catch(e){toast(e.message)}
    }
    if(type==="logs") {
      try {
        const d=await api("/api/audit-logs");
        c.innerHTML=`<div class="list-card"><h3>Audit Logs</h3><table><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Detail</th></tr>${d.logs.map(l=>`<tr><td>${escapeHtml(l.timestamp)}</td><td>${escapeHtml(l.user_name)}</td><td>${escapeHtml(l.action)}</td><td>${escapeHtml(l.target_type)} / ${escapeHtml(l.target_id)}</td><td>${escapeHtml(l.detail)}</td></tr>`).join("")}</table></div>`;
      }catch(e){toast(e.message)}
    }
  }

  function optionRows(options){
    return Object.entries(options||{}).map(([group,values])=>`<div class="option-group" data-group="${escapeAttr(group)}"><div class="table-row"><input class="option-group-name" value="${escapeAttr(group)}" placeholder="ชื่อกลุ่ม เช่น ขนาด"><button type="button" class="secondary" onclick="this.closest('.option-group').remove()">ลบกลุ่ม</button></div><div class="option-values">${(Array.isArray(values)?values:[]).map(v=>{const name=typeof v==="object"?v.name:v;const price=typeof v==="object"?v.price:0;return `<div class="option-value"><input class="option-value-name" value="${escapeAttr(name)}" placeholder="ค่าตัวเลือก"><input class="option-value-price" type="number" min="0" step="0.01" value="${Number(price||0)}" placeholder="ราคาเพิ่ม"><button type="button" class="secondary" onclick="this.parentElement.remove()">ลบ</button></div>`}).join("")}</div><button type="button" class="secondary" onclick="App.addOptionValue(this)">+ เพิ่มค่าตัวเลือก</button></div>`).join("");
  }
  function showMenuForm(menu={}) {
    $("modal").classList.remove("hidden");
    $("modal").innerHTML=`<div><h2>${menu.id?"Edit":"Add"} Menu</h2><form onsubmit="App.saveMenu(event,'${menu.id||""}')"><label>ชื่อเมนู<input id="mf-name" value="${escapeAttr(menu.name||"")}" required></label><label>หมวดหมู่<select id="mf-category" required><option value="อาหาร" ${menu.category==="อาหาร"||["Pasta","Pizza","Steak","Salad"].includes(menu.category)?"selected":""}>อาหาร</option><option value="ของหวาน" ${menu.category==="ของหวาน"?"selected":""}>ของหวาน</option><option value="เครื่องดื่ม" ${menu.category==="เครื่องดื่ม"?"selected":""}>เครื่องดื่ม</option></select></label><label>ราคาเมนูหลัก<input id="mf-price" type="number" min="0" step="0.01" value="${menu.price||""}" required></label><label>รูปเมนู<input id="mf-image-file" type="file" accept="image/png,image/jpeg,image/webp"><input id="mf-image" type="hidden" value="${escapeAttr(menu.image_url||"")}"><span class="small">รองรับ PNG/JPG/WEBP ขนาดไม่เกิน 2 MB</span></label><label><input id="mf-stock" type="checkbox" ${menu.is_out_of_stock?"checked":""}> สินค้าหมด</label><h3>ตัวเลือกพิเศษและราคาเพิ่ม</h3><div id="menu-options">${optionRows(menu.options)}</div><button type="button" class="secondary" onclick="App.addOptionGroup()">+ เพิ่มกลุ่มตัวเลือก</button><div class="actions"><button class="primary">Save</button><button type="button" class="secondary" onclick="App.closeModal()">Cancel</button></div></form></div>`;
  }
  function addOptionGroup(){const box=$("menu-options");box.insertAdjacentHTML("beforeend",`<div class="option-group"><div class="table-row"><input class="option-group-name" placeholder="ชื่อกลุ่ม เช่น ขนาด"><button type="button" class="secondary" onclick="this.closest('.option-group').remove()">ลบกลุ่ม</button></div><div class="option-values"></div><button type="button" class="secondary" onclick="App.addOptionValue(this)">+ เพิ่มค่าตัวเลือก</button></div>`)}
  function addOptionValue(button){button.previousElementSibling.insertAdjacentHTML("beforeend",`<div class="option-value"><input class="option-value-name" placeholder="ค่าตัวเลือก" required><input class="option-value-price" type="number" min="0" step="0.01" value="0" placeholder="ราคาเพิ่ม"><button type="button" class="secondary" onclick="this.parentElement.remove()">ลบ</button></div>`)}
  function readMenuOptions(){const options={};document.querySelectorAll("#menu-options .option-group").forEach(group=>{const name=group.querySelector(".option-group-name")?.value.trim();if(!name)return;options[name]=[...group.querySelectorAll(".option-value")].map(row=>({name:row.querySelector(".option-value-name")?.value.trim(),price:row.querySelector(".option-value-price")?.value||0})).filter(v=>v.name)});return options}
  async function saveMenu(e,id){e.preventDefault();try{let imageUrl=$("mf-image").value;const file=$("mf-image-file")?.files?.[0];if(file){if(file.size>2_000_000)throw new Error("ไฟล์รูปต้องมีขนาดไม่เกิน 2 MB");imageUrl=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error("อ่านไฟล์รูปไม่สำเร็จ"));reader.readAsDataURL(file)});const uploaded=await api("/api/uploads/menu-image",{method:"POST",body:JSON.stringify({filename:file.name,image_data:imageUrl})});imageUrl=uploaded.image_url;}const body={name:$("mf-name").value,category:$("mf-category").value,price:$("mf-price").value,image_url:imageUrl,is_out_of_stock:$("mf-stock").checked,options:readMenuOptions()};await api(id?`/api/menus/${id}`:"/api/menus",{method:id?"PATCH":"POST",body:JSON.stringify(body)});closeModal();toast("บันทึกเมนูสำเร็จ");adminPage("menus")}catch(x){toast(x.message)}}
  async function deleteMenu(id){if(!confirm("ลบเมนูนี้หรือไม่?"))return;try{await api(`/api/menus/${id}`,{method:"DELETE"});toast("ลบเมนูสำเร็จ");adminPage("menus")}catch(e){toast(e.message)}}
  function showStaffForm(){$("modal").classList.remove("hidden");$("modal").innerHTML=`<div><h2>Add Staff</h2><form onsubmit="App.saveStaff(event)"><label>ชื่อ<input id="sf-name" required></label><label>Email<input id="sf-email" type="email" required></label><label>Password<input id="sf-pass" type="password" minlength="6" required></label><div class="actions"><button class="primary">Create Staff</button><button type="button" class="secondary" onclick="App.closeModal()">Cancel</button></div></form></div>`}
  async function saveStaff(e){e.preventDefault();try{await api("/api/users/staff",{method:"POST",body:JSON.stringify({name:$("sf-name").value,email:$("sf-email").value,password:$("sf-pass").value})});closeModal();toast("เพิ่ม Staff สำเร็จ");adminPage("users")}catch(x){toast(x.message)}}
  async function toggleUser(id,active){try{await api(`/api/users/${id}`,{method:"PATCH",body:JSON.stringify({active})});toast("อัปเดตผู้ใช้แล้ว");adminPage("users")}catch(e){toast(e.message)}}
  function closeModal(){$("modal").classList.add("hidden");$("modal").innerHTML=""}
  function escapeHtml(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]))}
  function escapeAttr(v){return escapeHtml(v)}

  restore();
  return {go,authMode,submitAuth,logout,loadMenus,reserve,loadReservations,loadCustomer,requestMoveTable,submitMoveRequest,requestSplitBill,saveSplitOwners,updateMoveRequest,loadDashboard,cartAdd,clearCart,submitCart,requestBill,callStaff,loadCustomerTables,claimTable,confirmPartySize,cancelPartySize,addConfiguredToCart,loadStaff,loadTables,showCheckout,previewBill,doCheckout,splitByCustomer,resAction,showStaffReserve,saveStaffReserve,loadPos,renderPos,posChoose,posAddConfigured,posAdd,posClear,posSubmit,setTableStatus,setTableCapacity,showMove,doMove,showMerge,doMerge,showSplit,doSplit,updateKitchen,adminPage,showMenuForm,addOptionGroup,addOptionValue,saveMenu,deleteMenu,showStaffForm,saveStaff,toggleUser,closeModal,quickOrder};
})();
