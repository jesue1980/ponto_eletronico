if ("serviceWorker" in navigator && window.PONTO_SW_URL) {
  navigator.serviceWorker.register(window.PONTO_SW_URL).catch(() => {});
}

const pontoForm = document.getElementById("pontoForm");
if (pontoForm) {
  const deviceKey = "ponto_repp_device_id";
  let deviceId = localStorage.getItem(deviceKey);
  if (!deviceId) {
    deviceId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
    localStorage.setItem(deviceKey, deviceId);
  }

  document.getElementById("dispositivo_id").value = deviceId;

  navigator.geolocation.getCurrentPosition(pos => {
    latitude.value = pos.coords.latitude;
    longitude.value = pos.coords.longitude;
    precisao.value = pos.coords.accuracy;
    gpsStatus.textContent = pos.coords.latitude.toFixed(5) + ", " + pos.coords.longitude.toFixed(5);
  }, () => {
    gpsStatus.textContent = "não autorizado";
  }, {enableHighAccuracy: true, timeout: 15000});

  navigator.mediaDevices.getUserMedia({video: {facingMode: "user"}, audio: false}).then(stream => {
    video.srcObject = stream;
    camStatus.textContent = "ativa";
  }).catch(() => {
    camStatus.textContent = "não autorizada";
  });

  pontoForm.addEventListener("submit", () => {
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    selfie.value = canvas.toDataURL("image/jpeg", .82);
  });
}

const btnLocal = document.getElementById("capturarLocal");
if (btnLocal) {
  btnLocal.addEventListener("click", () => {
    localStatus.textContent = "Capturando localização...";
    navigator.geolocation.getCurrentPosition(pos => {
      localLatitude.value = pos.coords.latitude.toFixed(7);
      localLongitude.value = pos.coords.longitude.toFixed(7);
      localStatus.textContent = "Localização capturada com precisão aproximada de " + Math.round(pos.coords.accuracy) + " m.";
    }, () => {
      localStatus.textContent = "Não foi possível capturar. Autorize o GPS no navegador.";
    }, {enableHighAccuracy: true, timeout: 15000});
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
