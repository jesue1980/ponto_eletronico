if ("serviceWorker" in navigator && window.PONTO_SW_URL) {
  navigator.serviceWorker.register(window.PONTO_SW_URL).catch(() => {});
}

const pontoForm = document.getElementById("pontoForm");
if (pontoForm) {
  const GEO_MIN_DURATION_MS = 10000;
  const GEO_TARGET_READINGS = 5;
  const GEO_MAX_ACCURACY = 50;
  const GEO_BLOCK_TEXT = "Ponto bloqueado: a localização precisa permanecer ativa e válida durante todo o processo de verificação.";
  const deviceKey = "ponto_repp_device_id";
  let deviceId = localStorage.getItem(deviceKey);
  let geoWatchId = null;
  let geoReadings = [];
  let geoStartedAt = 0;
  let geoApproved = false;
  let geoFailed = false;
  let cameraStream = null;
  if (!deviceId) {
    deviceId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
    localStorage.setItem(deviceKey, deviceId);
  }

  document.getElementById("dispositivo_id").value = deviceId;
  const registerButton = document.getElementById("registrarBtn") || pontoForm.querySelector("button[type='submit']");
  const geoBlockMessage = document.getElementById("geoBlockMessage");

  function setBlocked(text) {
    geoApproved = false;
    if (registerButton) registerButton.disabled = true;
    if (geoBlockMessage) geoBlockMessage.textContent = text || GEO_BLOCK_TEXT;
  }

  function markGeoFailure(text, suspicious = false) {
    geoFailed = true;
    geo_falha_permissao.value = "1";
    if (suspicious) geo_mock_suspeito.value = "1";
    setBlocked(text || GEO_BLOCK_TEXT);
    gpsStatus.textContent = "bloqueado";
  }

  function syncGeoFields() {
    const first = geoReadings[0];
    const last = geoReadings[geoReadings.length - 1];
    if (!last) return;
    latitude.value = last.latitude;
    longitude.value = last.longitude;
    precisao.value = last.precisao;
    geo_primeira_leitura_em.value = first ? new Date(first.capturada_em).toISOString() : "";
    geo_ultima_leitura_em.value = new Date(last.capturada_em).toISOString();
    geo_tempo_validacao_seg.value = first ? String(Math.floor((last.capturada_em - first.capturada_em) / 1000)) : "0";
    geo_qtd_leituras.value = String(geoReadings.length);
    geo_leituras_json.value = JSON.stringify(geoReadings);
  }

  async function startCameraAfterGeo() {
    if (cameraStream) return;
    cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
    video.srcObject = cameraStream;
    camStatus.textContent = "ativa";
  }

  function approveGeoIfReady() {
    const first = geoReadings[0];
    const last = geoReadings[geoReadings.length - 1];
    if (!first || !last || geoFailed) return;
    const elapsed = last.capturada_em - first.capturada_em;
    const remaining = Math.max(0, Math.ceil((GEO_MIN_DURATION_MS - elapsed) / 1000));
    gpsStatus.textContent = `${geoReadings.length}/${GEO_TARGET_READINGS} leituras - ${Math.round(last.precisao)} m - ${remaining}s`;
    if (geoReadings.length >= GEO_TARGET_READINGS && elapsed >= GEO_MIN_DURATION_MS) {
      geoApproved = true;
      syncGeoFields();
      gpsStatus.textContent = `${last.latitude.toFixed(5)}, ${last.longitude.toFixed(5)} (${Math.round(last.precisao)} m)`;
      if (registerButton) registerButton.disabled = false;
      if (geoBlockMessage) geoBlockMessage.textContent = "Localização validada continuamente. Mantenha esta tela aberta ate concluir.";
      startCameraAfterGeo().catch(() => {
        camStatus.textContent = "nao autorizada";
        setBlocked("Camera nao autorizada. Autorize a camera para registrar o ponto.");
      });
    }
  }

  function startContinuousGeoValidation() {
    if (!navigator.geolocation) {
      markGeoFailure("GPS indisponivel neste navegador.");
      return;
    }
    setBlocked(GEO_BLOCK_TEXT);
    gpsStatus.textContent = "validando 10s...";
    geoStartedAt = Date.now();
    geoReadings = [];
    geoApproved = false;
    geoFailed = false;
    geo_falha_permissao.value = "0";
    geo_mock_suspeito.value = "0";
    geo_leituras_json.value = "";
    if (geoWatchId !== null) navigator.geolocation.clearWatch(geoWatchId);
    geoWatchId = navigator.geolocation.watchPosition(pos => {
      const mockSuspect = Boolean(pos.coords.mocked || pos.mocked);
      if (mockSuspect) {
        markGeoFailure(GEO_BLOCK_TEXT, true);
        return;
      }
      if (!Number.isFinite(pos.coords.accuracy) || pos.coords.accuracy > GEO_MAX_ACCURACY) {
        markGeoFailure("Precisao do GPS insuficiente. Ative alta precisao e tente novamente.");
        return;
      }
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
      markGeoFailure(error.message || GEO_BLOCK_TEXT);
    }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden && geoStartedAt && !geoApproved) {
      markGeoFailure(GEO_BLOCK_TEXT);
    }
  });
  window.addEventListener("pagehide", () => {
    if (geoStartedAt && !geoApproved) markGeoFailure(GEO_BLOCK_TEXT);
  });

  startContinuousGeoValidation();

  pontoForm.addEventListener("submit", event => {
    if (!geoApproved || geoFailed || !geo_leituras_json.value) {
      event.preventDefault();
      setBlocked(GEO_BLOCK_TEXT);
      return;
    }
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    selfie.value = canvas.toDataURL("image/jpeg", .82);
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
