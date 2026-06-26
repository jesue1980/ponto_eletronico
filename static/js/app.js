if ("serviceWorker" in navigator && window.PONTO_SW_URL) {
  navigator.serviceWorker.register(window.PONTO_SW_URL).catch(() => {});
}

const mobileMenu = document.getElementById("mobileMenu");
const mobileMenuToggle = document.querySelector(".mobile-menu-toggle");
const mobileMenuClosers = document.querySelectorAll("[data-mobile-menu-close]");
if (mobileMenu && mobileMenuToggle) {
  function closeMobileMenu() {
    document.body.classList.remove("mobile-menu-open");
    mobileMenu.setAttribute("aria-hidden", "true");
    mobileMenuToggle.setAttribute("aria-expanded", "false");
  }

  function openMobileMenu() {
    document.body.classList.add("mobile-menu-open");
    mobileMenu.setAttribute("aria-hidden", "false");
    mobileMenuToggle.setAttribute("aria-expanded", "true");
  }

  mobileMenuToggle.addEventListener("click", () => {
    if (document.body.classList.contains("mobile-menu-open")) {
      closeMobileMenu();
    } else {
      openMobileMenu();
    }
  });

  mobileMenuClosers.forEach(item => item.addEventListener("click", closeMobileMenu));
  mobileMenu.querySelectorAll("a").forEach(link => link.addEventListener("click", closeMobileMenu));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeMobileMenu();
  });
}

const pontoForm = document.getElementById("pontoForm");
if (pontoForm) {
  const GEO_MIN_DURATION_MS = 10000;
  const GEO_TARGET_READINGS = 5;
  const GEO_MAX_ACCURACY = 50;
  const GEO_MAX_WAIT_MS = 45000;
  const GEO_BLOCK_TEXT = "Ponto bloqueado: a localizacao precisa permanecer ativa e valida durante todo o processo de verificacao.";
  const SECURE_CONTEXT_TEXT = "Para usar câmera e localização no celular, acesse por HTTPS. Navegadores bloqueiam câmera/GPS em HTTP.";
  const LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1"];
  const isLocalHost = LOCAL_HOSTS.includes(window.location.hostname);
  const isSecureOrigin = window.isSecureContext || isLocalHost;
  const isMobileOrTablet = matchMedia("(pointer: coarse)").matches || /Android|iPhone|iPad|iPod|Mobile|Tablet/i.test(navigator.userAgent);
  const suggestedHttpsUrl = `https://${window.location.hostname}:5443${window.location.pathname}`;
  const deviceKey = "ponto_repp_device_id";

  let deviceId = localStorage.getItem(deviceKey);
  let geoWatchId = null;
  let geoReadings = [];
  let geoStartedAt = 0;
  let geoApproved = false;
  let geoFailed = false;
  let cameraApproved = false;
  let cameraLoaded = false;
  let cameraStream = null;
  let mobileActivationButton = null;
  let checklistBox = null;

  if (!deviceId) {
    deviceId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
    localStorage.setItem(deviceKey, deviceId);
  }

  const videoEl = document.getElementById("video");
  const canvasEl = document.getElementById("canvas");
  const latitudeInput = document.getElementById("latitude");
  const longitudeInput = document.getElementById("longitude");
  const precisaoInput = document.getElementById("precisao");
  const geoPrimeiraInput = document.getElementById("geo_primeira_leitura_em");
  const geoUltimaInput = document.getElementById("geo_ultima_leitura_em");
  const geoTempoInput = document.getElementById("geo_tempo_validacao_seg");
  const geoQtdInput = document.getElementById("geo_qtd_leituras");
  const geoFalhaInput = document.getElementById("geo_falha_permissao");
  const geoMockInput = document.getElementById("geo_mock_suspeito");
  const geoLeiturasInput = document.getElementById("geo_leituras_json");
  const selfieInput = document.getElementById("selfie");
  const dispositivoInput = document.getElementById("dispositivo_id");
  const gpsStatusEl = document.getElementById("gpsStatus");
  const camStatusEl = document.getElementById("camStatus");
  const deviceStatusEl = document.getElementById("deviceStatus");
  const registerButton = document.getElementById("registrarBtn") || pontoForm.querySelector("button[type='submit']");
  const geoBlockMessage = document.getElementById("geoBlockMessage");

  const checklistState = {
    secure: isSecureOrigin,
    cameraPermission: false,
    locationPermission: false,
    gpsActive: false,
    cameraLoaded: false,
    device: Boolean(deviceId)
  };

  dispositivoInput.value = deviceId;
  if (deviceStatusEl) deviceStatusEl.textContent = deviceId ? "registrado" : "nao identificado";

  function friendlyCameraError(error) {
    if (!isSecureOrigin) return SECURE_CONTEXT_TEXT;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return "Camera indisponivel neste navegador. Atualize o navegador ou use Chrome/Safari atual.";
    }
    if (error && (error.name === "NotAllowedError" || error.name === "PermissionDeniedError")) {
      return "Camera bloqueada. Toque no cadeado do navegador e permita camera para este site.";
    }
    if (error && (error.name === "NotFoundError" || error.name === "DevicesNotFoundError")) {
      return "Nenhuma camera foi encontrada. Verifique se o celular possui camera ativa.";
    }
    if (error && (error.name === "NotReadableError" || error.name === "TrackStartError")) {
      return "A camera esta em uso por outro aplicativo. Feche outros apps e tente novamente.";
    }
    return "Nao foi possivel iniciar a camera. Permita camera no navegador e tente novamente.";
  }

  function friendlyGeoError(error) {
    if (!isSecureOrigin) return SECURE_CONTEXT_TEXT;
    if (!navigator.geolocation) return "GPS indisponivel neste navegador. Use Chrome/Safari atual.";
    if (error && error.code === error.PERMISSION_DENIED) {
      return "Localizacao bloqueada. Permita localizacao no navegador para este site.";
    }
    if (error && error.code === error.POSITION_UNAVAILABLE) {
      return "GPS indisponivel. Ative a localizacao do celular e o modo alta precisao.";
    }
    if (error && error.code === error.TIMEOUT) {
      return "GPS demorou para responder. Ative a localizacao do celular e fique em area com sinal.";
    }
    return "Nao foi possivel capturar GPS. Ative a localizacao do celular e permita localizacao no navegador.";
  }

  function createChecklist() {
    checklistBox = document.createElement("div");
    checklistBox.className = "mobile-permission-checklist small mb-3";
    checklistBox.innerHTML = [
      '<div data-check="secure"><span>HTTPS</span><strong>verificando</strong></div>',
      '<div data-check="cameraPermission"><span>Permissao da camera</span><strong>verificando</strong></div>',
      '<div data-check="locationPermission"><span>Permissao da localizacao</span><strong>verificando</strong></div>',
      '<div data-check="gpsActive"><span>GPS ativo</span><strong>verificando</strong></div>',
      '<div data-check="cameraLoaded"><span>Camera carregada</span><strong>verificando</strong></div>',
      '<div data-check="device"><span>Dispositivo</span><strong>verificando</strong></div>'
    ].join("");
    geoBlockMessage.parentNode.insertBefore(checklistBox, geoBlockMessage);
  }

  function updateChecklist() {
    if (!checklistBox) return;
    Object.keys(checklistState).forEach(key => {
      const row = checklistBox.querySelector(`[data-check="${key}"]`);
      if (!row) return;
      row.classList.toggle("ok", Boolean(checklistState[key]));
      row.classList.toggle("fail", !checklistState[key]);
      row.querySelector("strong").textContent = checklistState[key] ? "OK" : "pendente";
    });
  }

  function setBlocked(text, options = {}) {
    geoApproved = false;
    if (!options.keepCamera) {
      cameraApproved = false;
    }
    if (registerButton) registerButton.disabled = true;
    if (geoBlockMessage) geoBlockMessage.textContent = text || GEO_BLOCK_TEXT;
    updateChecklist();
  }

  function syncRegisterButton() {
    const ready = checklistState.secure &&
      checklistState.cameraPermission &&
      checklistState.locationPermission &&
      checklistState.gpsActive &&
      checklistState.cameraLoaded &&
      checklistState.device &&
      geoApproved &&
      cameraApproved &&
      cameraLoaded &&
      !geoFailed;
    if (registerButton) registerButton.disabled = !ready;
    updateChecklist();
  }

  function showMobileRetryButton() {
    if (!isMobileOrTablet || !mobileActivationButton) return;
    mobileActivationButton.classList.remove("d-none");
    mobileActivationButton.disabled = false;
    mobileActivationButton.innerHTML = '<i class="bi bi-phone"></i> Tentar camera e GPS novamente';
  }

  function markGeoFailure(text, suspicious = false) {
    geoFailed = true;
    checklistState.locationPermission = false;
    checklistState.gpsActive = false;
    geoFalhaInput.value = "1";
    if (suspicious) geoMockInput.value = "1";
    setBlocked(text || GEO_BLOCK_TEXT, { keepCamera: isMobileOrTablet && cameraApproved });
    gpsStatusEl.textContent = "bloqueado";
    showMobileRetryButton();
  }

  function syncGeoFields() {
    const first = geoReadings[0];
    const last = geoReadings[geoReadings.length - 1];
    if (!last) return;
    latitudeInput.value = last.latitude;
    longitudeInput.value = last.longitude;
    precisaoInput.value = last.precisao;
    geoPrimeiraInput.value = first ? new Date(first.capturada_em).toISOString() : "";
    geoUltimaInput.value = new Date(last.capturada_em).toISOString();
    geoTempoInput.value = first ? String(Math.floor((last.capturada_em - first.capturada_em) / 1000)) : "0";
    geoQtdInput.value = String(geoReadings.length);
    geoLeiturasInput.value = JSON.stringify(geoReadings);
  }

  async function startCamera() {
    if (!isSecureOrigin || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error(SECURE_CONTEXT_TEXT);
    }
    if (cameraStream) {
      cameraApproved = true;
      checklistState.cameraPermission = true;
      syncRegisterButton();
      return;
    }
    try {
      cameraStream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: "user",
          width: { ideal: 640 },
          height: { ideal: 480 }
        },
        audio: false
      });
    } catch (error) {
      checklistState.cameraPermission = false;
      checklistState.cameraLoaded = false;
      throw new Error(friendlyCameraError(error));
    }
    videoEl.srcObject = cameraStream;
    try {
      await videoEl.play();
    } catch (_error) {}
    cameraApproved = true;
    checklistState.cameraPermission = true;
    camStatusEl.textContent = "carregando...";
    await new Promise(resolve => {
      if (videoEl.readyState >= 2 && videoEl.videoWidth) {
        resolve();
        return;
      }
      videoEl.onloadedmetadata = () => resolve();
      setTimeout(resolve, 2500);
    });
    cameraLoaded = Boolean(videoEl.videoWidth || videoEl.readyState >= 2);
    checklistState.cameraLoaded = cameraLoaded;
    camStatusEl.textContent = cameraLoaded ? "ativa" : "aguardando video";
    syncRegisterButton();
  }

  function approveGeoIfReady() {
    const first = geoReadings[0];
    const last = geoReadings[geoReadings.length - 1];
    if (!first || !last || geoFailed) return;
    const elapsed = last.capturada_em - first.capturada_em;
    const remaining = Math.max(0, Math.ceil((GEO_MIN_DURATION_MS - elapsed) / 1000));
    gpsStatusEl.textContent = `${geoReadings.length}/${GEO_TARGET_READINGS} leituras - ${Math.round(last.precisao)} m - ${remaining}s`;
    if (geoReadings.length >= GEO_TARGET_READINGS && elapsed >= GEO_MIN_DURATION_MS) {
      geoApproved = true;
      checklistState.locationPermission = true;
      checklistState.gpsActive = true;
      syncGeoFields();
      gpsStatusEl.textContent = `${last.latitude.toFixed(5)}, ${last.longitude.toFixed(5)} (${Math.round(last.precisao)} m)`;
      syncRegisterButton();
      if (geoBlockMessage) geoBlockMessage.textContent = "Localizacao validada continuamente. Mantenha esta tela aberta ate concluir.";
      if (!isMobileOrTablet) {
        startCamera().catch(error => {
          cameraApproved = false;
          checklistState.cameraPermission = false;
          checklistState.cameraLoaded = false;
          camStatusEl.textContent = "nao autorizada";
          setBlocked(error.message || "Camera nao autorizada. Permita camera no navegador para registrar o ponto.");
        });
      }
    }
  }

  function startContinuousGeoValidation() {
    if (!isSecureOrigin) {
      markGeoFailure(SECURE_CONTEXT_TEXT);
      return;
    }
    if (!navigator.geolocation) {
      markGeoFailure("GPS indisponivel neste navegador.");
      return;
    }
    setBlocked(GEO_BLOCK_TEXT, { keepCamera: isMobileOrTablet && cameraApproved });
    gpsStatusEl.textContent = "validando 10s...";
    geoStartedAt = Date.now();
    geoReadings = [];
    geoApproved = false;
    if (!isMobileOrTablet || !cameraStream) cameraApproved = false;
    geoFailed = false;
    checklistState.locationPermission = false;
    checklistState.gpsActive = false;
    geoFalhaInput.value = "0";
    geoMockInput.value = "0";
    geoLeiturasInput.value = "";
    if (geoWatchId !== null) navigator.geolocation.clearWatch(geoWatchId);
    geoWatchId = navigator.geolocation.watchPosition(pos => {
      const mockSuspect = Boolean(pos.coords.mocked || pos.mocked);
      if (mockSuspect) {
        markGeoFailure(GEO_BLOCK_TEXT, true);
        return;
      }
      if (!Number.isFinite(pos.coords.accuracy) || pos.coords.accuracy > GEO_MAX_ACCURACY) {
        checklistState.locationPermission = true;
        gpsStatusEl.textContent = `aguardando precisao - ${Number.isFinite(pos.coords.accuracy) ? Math.round(pos.coords.accuracy) : "--"} m`;
        updateChecklist();
        if (Date.now() - geoStartedAt > GEO_MAX_WAIT_MS && geoReadings.length === 0) {
          markGeoFailure("Precisao do GPS insuficiente. Ative a localizacao do celular, use alta precisao e tente novamente.");
        }
        return;
      }
      checklistState.locationPermission = true;
      const reading = {
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
        precisao: pos.coords.accuracy,
        capturada_em: Date.now(),
        mock_suspeito: mockSuspect
      };
      geoReadings.push(reading);
      if (geoReadings.length > GEO_TARGET_READINGS) geoReadings = geoReadings.slice(-GEO_TARGET_READINGS);
      syncGeoFields();
      approveGeoIfReady();
    }, error => {
      markGeoFailure(friendlyGeoError(error));
    }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden && geoStartedAt && geoReadings.length > 0 && !geoApproved) {
      markGeoFailure(GEO_BLOCK_TEXT);
    }
  });
  window.addEventListener("pagehide", () => {
    if (geoStartedAt && geoReadings.length > 0 && !geoApproved) markGeoFailure(GEO_BLOCK_TEXT);
  });

  function createMobileActivationButton() {
    if (!isMobileOrTablet || mobileActivationButton) return;
    mobileActivationButton = document.createElement("button");
    mobileActivationButton.type = "button";
    mobileActivationButton.className = "btn btn-primary btn-lg w-100 mb-3";
    mobileActivationButton.innerHTML = '<i class="bi bi-phone"></i> Ativar camera e GPS no celular';
    mobileActivationButton.addEventListener("click", () => {
      mobileActivationButton.disabled = true;
      mobileActivationButton.textContent = "Ativando camera e GPS...";
      camStatusEl.textContent = "solicitando...";
      gpsStatusEl.textContent = "solicitando...";
      Promise.allSettled([
        startCamera(),
        Promise.resolve().then(startContinuousGeoValidation)
      ]).then(results => {
        const cameraResult = results[0];
        if (cameraResult.status === "rejected") {
          cameraApproved = false;
          checklistState.cameraPermission = false;
          checklistState.cameraLoaded = false;
          camStatusEl.textContent = "nao autorizada";
          setBlocked(cameraResult.reason && cameraResult.reason.message ? cameraResult.reason.message : friendlyCameraError(cameraResult.reason));
          mobileActivationButton.disabled = false;
          mobileActivationButton.innerHTML = '<i class="bi bi-phone"></i> Tentar camera e GPS novamente';
          return;
        }
        if (geoFailed) {
          showMobileRetryButton();
          syncRegisterButton();
          return;
        }
        mobileActivationButton.classList.add("d-none");
        syncRegisterButton();
      });
    });
    checklistBox.parentNode.insertBefore(mobileActivationButton, checklistBox);
    gpsStatusEl.textContent = "toque em ativar";
    camStatusEl.textContent = "toque em ativar";
    if (geoBlockMessage) geoBlockMessage.textContent = "No celular, toque no botao acima e permita camera e localizacao quando o navegador solicitar.";
  }

  createChecklist();
  updateChecklist();
  if (!isSecureOrigin && !isLocalHost) {
    geoBlockMessage.classList.remove("alert-warning");
    geoBlockMessage.classList.add("alert-danger");
    setBlocked(`${SECURE_CONTEXT_TEXT} Tente abrir: ${suggestedHttpsUrl}`);
    gpsStatusEl.textContent = "HTTPS obrigatorio";
    camStatusEl.textContent = "HTTPS obrigatorio";
  }

  if (isMobileOrTablet && isSecureOrigin) {
    createMobileActivationButton();
  } else if (isSecureOrigin) {
    startContinuousGeoValidation();
  }

  pontoForm.addEventListener("submit", event => {
    const blockedReason = !isSecureOrigin ? SECURE_CONTEXT_TEXT :
      !cameraApproved ? "Camera bloqueada. Permita camera no navegador." :
      !cameraLoaded ? "Camera ainda nao carregou. Aguarde a imagem aparecer." :
      !geoApproved ? "GPS ainda nao validado. Ative a localizacao do celular e permita localizacao no navegador." :
      !deviceId ? "Dispositivo nao identificado. Atualize a pagina e tente novamente." :
      GEO_BLOCK_TEXT;
    if (!geoApproved || !cameraApproved || !cameraLoaded || geoFailed || !geoLeiturasInput.value || !deviceId || !isSecureOrigin) {
      event.preventDefault();
      setBlocked(blockedReason, { keepCamera: isMobileOrTablet && cameraApproved });
      return;
    }
    canvasEl.width = videoEl.videoWidth || 640;
    canvasEl.height = videoEl.videoHeight || 480;
    canvasEl.getContext("2d").drawImage(videoEl, 0, 0, canvasEl.width, canvasEl.height);
    selfieInput.value = canvasEl.toDataURL("image/jpeg", .82);
  });
}

const diagnosticoMobile = document.getElementById("diagnosticoMobile");
if (diagnosticoMobile) {
  const LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1"];
  const isSecureOrigin = window.isSecureContext || LOCAL_HOSTS.includes(window.location.hostname);
  const deviceKey = "ponto_repp_device_id";
  let deviceId = localStorage.getItem(deviceKey);
  if (!deviceId) {
    deviceId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
    localStorage.setItem(deviceKey, deviceId);
  }

  const rows = {
    https: document.querySelector('[data-diag="https"] strong'),
    camera: document.querySelector('[data-diag="camera"] strong'),
    gps: document.querySelector('[data-diag="gps"] strong'),
    browser: document.querySelector('[data-diag="browser"] strong'),
    os: document.querySelector('[data-diag="os"] strong'),
    device: document.querySelector('[data-diag="device"] strong')
  };
  const output = document.getElementById("diagnosticoResultado");
  const button = document.getElementById("diagnosticoPermissoes");

  function detectOs() {
    const ua = navigator.userAgent;
    if (/Android/i.test(ua)) return "Android";
    if (/iPhone|iPad|iPod/i.test(ua)) return "iOS/iPadOS";
    if (/Windows/i.test(ua)) return "Windows";
    if (/Macintosh|Mac OS X/i.test(ua)) return "macOS";
    if (/Linux/i.test(ua)) return "Linux";
    return "Nao identificado";
  }

  function detectBrowser() {
    const ua = navigator.userAgent;
    if (/CriOS/i.test(ua)) return "Chrome iOS";
    if (/Edg/i.test(ua)) return "Edge";
    if (/Chrome/i.test(ua) && /Android/i.test(ua)) return "Chrome Android";
    if (/Safari/i.test(ua) && !/Chrome|CriOS|Edg/i.test(ua)) return "Safari";
    if (/Firefox/i.test(ua)) return "Firefox";
    if (/Chrome/i.test(ua)) return "Chrome";
    return navigator.userAgent;
  }

  function setDiag(row, ok, text) {
    if (!row) return;
    row.textContent = text;
    row.parentElement.classList.toggle("ok", Boolean(ok));
    row.parentElement.classList.toggle("fail", !ok);
  }

  setDiag(rows.https, isSecureOrigin, isSecureOrigin ? "ativo" : "inativo");
  setDiag(rows.browser, true, detectBrowser());
  setDiag(rows.os, true, detectOs());
  setDiag(rows.device, Boolean(deviceId), deviceId ? "registrado" : "nao identificado");
  setDiag(rows.camera, false, "nao testada");
  setDiag(rows.gps, false, "nao testado");

  button.addEventListener("click", async () => {
    button.disabled = true;
    output.textContent = "Testando permissoes...";
    if (!isSecureOrigin) {
      const message = "Para usar câmera e localização no celular, acesse por HTTPS. Navegadores bloqueiam câmera/GPS em HTTP.";
      setDiag(rows.camera, false, "bloqueada por HTTP");
      setDiag(rows.gps, false, "bloqueado por HTTP");
      output.textContent = message;
      button.disabled = false;
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
      stream.getTracks().forEach(track => track.stop());
      setDiag(rows.camera, true, "disponivel e permitida");
    } catch (error) {
      setDiag(rows.camera, false, error && error.name === "NotAllowedError" ? "bloqueada pelo navegador" : "indisponivel");
    }

    await new Promise(resolve => {
      if (!navigator.geolocation) {
        setDiag(rows.gps, false, "indisponivel");
        resolve();
        return;
      }
      navigator.geolocation.getCurrentPosition(pos => {
        setDiag(rows.gps, true, `permitido (${Math.round(pos.coords.accuracy)} m)`);
        resolve();
      }, error => {
        const message = error.code === error.PERMISSION_DENIED ? "bloqueado pelo navegador" :
          error.code === error.POSITION_UNAVAILABLE ? "GPS desligado/sem sinal" :
          "sem resposta";
        setDiag(rows.gps, false, message);
        resolve();
      }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 });
    });

    output.textContent = "Diagnostico concluido.";
    button.disabled = false;
  });
}

const btnLocal = document.getElementById("capturarLocal");
if (btnLocal) {
  btnLocal.addEventListener("click", () => {
    localStatus.textContent = "Capturando localizacao...";
    navigator.geolocation.getCurrentPosition(pos => {
      localLatitude.value = pos.coords.latitude.toFixed(7);
      localLongitude.value = pos.coords.longitude.toFixed(7);
      localStatus.textContent = "Localizacao capturada com precisao aproximada de " + Math.round(pos.coords.accuracy) + " m.";
    }, () => {
      localStatus.textContent = "Nao foi possivel capturar. Autorize o GPS no navegador.";
    }, { enableHighAccuracy: true, timeout: 15000 });
  });
}

const tipoJornada = document.getElementById("tipoJornada");
if (tipoJornada) {
  const entrada = document.getElementById("horarioEntrada");
  const saidaAlmoco = document.getElementById("horarioSaidaAlmoco");
  const retornoAlmoco = document.getElementById("horarioRetornoAlmoco");
  const saidaFinal = document.getElementById("horarioSaidaFinal");

  tipoJornada.addEventListener("change", () => {
    if (tipoJornada.value === "8 horas com intervalo") {
      entrada.value = "07:00";
      saidaAlmoco.value = "11:00";
      retornoAlmoco.value = "13:00";
      saidaFinal.value = "17:00";
    } else if (tipoJornada.value === "6 horas corridas") {
      entrada.value = "07:00";
      saidaAlmoco.value = "";
      retornoAlmoco.value = "";
      saidaFinal.value = "13:00";
    } else if (tipoJornada.value === "4 horas corridas") {
      entrada.value = "07:00";
      saidaAlmoco.value = "";
      retornoAlmoco.value = "";
      saidaFinal.value = "11:00";
    }
  });
}
