/**
 * TVCertificates - tab toggle for the Certificates page (certificates.html).
 * Same module pattern as the other js/ files.
 */
window.TVCertificates = (function () {
  function init(ids) {
    const tabMine = document.getElementById(ids.tabMineId);
    const tabAll = document.getElementById(ids.tabAllId);
    const panelMine = document.getElementById(ids.panelMineId);
    const panelAll = document.getElementById(ids.panelAllId);
    if (!tabMine || !tabAll || !panelMine || !panelAll) return null;

    function showMine() {
      tabMine.classList.add("is-active");
      tabAll.classList.remove("is-active");
      panelMine.hidden = false;
      panelAll.hidden = true;
    }

    function showAll() {
      tabAll.classList.add("is-active");
      tabMine.classList.remove("is-active");
      panelAll.hidden = false;
      panelMine.hidden = true;
    }

    tabMine.addEventListener("click", showMine);
    tabAll.addEventListener("click", showAll);
  }

  return { init };
})();
