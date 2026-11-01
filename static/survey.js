const form = document.getElementById("survey-form");
if (form) {
  const slides = [...form.querySelectorAll(".quiz-slide")];
  const total = slides.length;
  const limit = Number(form.dataset.seconds || 0);
  const progressEl = document.getElementById("quiz-progress");
  const hintEl = document.getElementById("quiz-hint");
  const kickerEl = document.getElementById("quiz-kicker");
  const skipBtn = document.getElementById("skip-btn");
  const answerBtn = document.getElementById("answer-btn");
  const pickedEl = document.getElementById("picked-count");
  const skippedPanel = document.getElementById("skipped-panel");
  const skippedLead = document.getElementById("skipped-lead");
  const reviewBtn = document.getElementById("review-btn");
  const quizMain = form.querySelector(".quiz-main");
  const quizSide = form.querySelector(".quiz-side");
  const timerNum = document.getElementById("timer-num");
  const timerProgress = document.getElementById("timer-progress");
  const CIRC = 2 * Math.PI * 52;

  let queue = slides.map((_, i) => i);
  let pos = 0;
  let skipped = [];
  let reviewing = false;
  let remain = limit;
  let tickId = null;
  let spentMs = 0;
  let sliceStart = 0;
  let sliceOn = false;
  const spentInput = document.getElementById("spent-seconds");

  function pauseSlice() {
    if (!sliceOn) return;
    spentMs += Date.now() - sliceStart;
    sliceOn = false;
  }

  function resumeSlice() {
    pauseSlice();
    sliceStart = Date.now();
    sliceOn = true;
  }

  function writeSpent() {
    pauseSlice();
    const seconds = Math.max(0, Math.round(spentMs / 1000));
    if (spentInput) spentInput.value = String(seconds);
    return seconds;
  }

  if (timerProgress) {
    timerProgress.style.strokeDasharray = String(CIRC);
    timerProgress.style.strokeDashoffset = "0";
  }

  function currentSlide() {
    return slides[queue[pos]];
  }

  function typeHint(slide) {
    const type = slide.dataset.type;
    if (type === "open") return "открытый ответ";
    if (type === "single") return "один вариант";
    return "можно несколько вариантов";
  }

  function selectedCount(slide) {
    if (slide.dataset.type === "open") {
      return slide.querySelector(".quiz-open")?.value.trim() ? 1 : 0;
    }
    return slide.querySelectorAll("input:checked").length;
  }

  function updateAnswerLabel() {
    const slide = currentSlide();
    if (!slide || !pickedEl) return;
    const n = selectedCount(slide);
    if (slide.dataset.type === "multiple") {
      pickedEl.textContent = n ? `Выбрано: ${n}` : "Выбрано: 0";
    } else {
      pickedEl.textContent = "";
    }
    answerBtn.classList.toggle("is-ready", n > 0);
  }

  function setRing() {
    if (!timerProgress || !limit) return;
    const ratio = Math.max(0, remain / limit);
    timerProgress.style.strokeDashoffset = String(CIRC * (1 - ratio));
    timerProgress.classList.toggle("is-low", remain <= 5 && remain > 0);
  }

  function stopTimer() {
    if (tickId) {
      clearInterval(tickId);
      tickId = null;
    }
  }

  function startTimer() {
    stopTimer();
    if (!limit || !timerNum) return;
    remain = limit;
    timerNum.textContent = String(remain);
    setRing();
    tickId = setInterval(() => {
      remain -= 1;
      if (remain <= 0) {
        timerNum.textContent = "0";
        setRing();
        stopTimer();
        skipCurrent();
        return;
      }
      timerNum.textContent = String(remain);
      setRing();
    }, 1000);
  }

  function showPos() {
    slides.forEach((slide) => {
      slide.hidden = true;
    });
    const slide = currentSlide();
    if (!slide) return;
    slide.hidden = false;
    const human = reviewing ? `Пропущенный ${pos + 1} из ${queue.length}` : `Вопрос ${queue[pos] + 1} из ${total}`;
    progressEl.textContent = human;
    hintEl.textContent = typeHint(slide);
    kickerEl.textContent = reviewing ? "Пропущенные" : "Опрос";
    updateAnswerLabel();
    resumeSlice();
    startTimer();
  }

  function goNext() {
    pos += 1;
    if (pos < queue.length) {
      showPos();
      return;
    }
    if (skipped.length) {
      stopTimer();
      pauseSlice();
      quizMain.hidden = true;
      if (quizSide) quizSide.hidden = true;
      skippedPanel.hidden = false;
      skippedLead.textContent =
        skipped.length === 1
          ? "Пропущен 1 вопрос. Можно ответить сейчас или завершить анкету."
          : `Пропущено вопросов: ${skipped.length}. Можно ответить сейчас или завершить анкету.`;
      return;
    }
    stopTimer();
    writeSpent();
    form.submit();
  }

  function skipCurrent() {
    const idx = queue[pos];
    if (!skipped.includes(idx)) skipped.push(idx);
    goNext();
  }

  function answerCurrent() {
    const slide = currentSlide();
    if (!selectedCount(slide)) {
      hintEl.textContent = "Сначала выберите ответ или нажмите «Пропустить»";
      return;
    }
    skipped = skipped.filter((i) => i !== queue[pos]);
    goNext();
  }

  skipBtn?.addEventListener("click", () => skipCurrent());
  answerBtn?.addEventListener("click", () => answerCurrent());
  form.addEventListener("submit", () => {
    writeSpent();
  });
  reviewBtn?.addEventListener("click", () => {
    queue = [...skipped];
    skipped = [];
    pos = 0;
    reviewing = true;
    skippedPanel.hidden = true;
    quizMain.hidden = false;
    if (quizSide) quizSide.hidden = false;
    showPos();
  });

  form.addEventListener("change", updateAnswerLabel);
  form.addEventListener("input", updateAnswerLabel);
  form.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    if (event.target.matches(".quiz-open")) {
      event.preventDefault();
      answerCurrent();
    }
  });

  slides.forEach((slide) => {
    if (slide.dataset.type !== "single") return;
    slide.querySelectorAll(".quiz-option").forEach((label) => {
      label.addEventListener("click", () => {
        slide.querySelectorAll(".quiz-option").forEach((other) => other.classList.remove("is-on"));
        label.classList.add("is-on");
      });
    });
  });

  showPos();
}
