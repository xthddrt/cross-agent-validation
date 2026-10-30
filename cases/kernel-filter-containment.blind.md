Reconcile two independent implementations of application containment for
kernel-filter into one implementation. Both were built on main at eff419c.

kernel-filter is a Windows kernel driver (minifilter + registry/object/process callbacks),
a control tool (kfilterctl) and policy tooling that limit what the MapleStoryCW client and its
approved descendants can access outside their own resources on the user's own machine.

The objective is one implementation that delivers this containment design:

1. Rights-based policy: independent read / write / execute / delete / rename / set-security /
   enumerate rights; file, directory, registry (including value= matching), process, volume,
   device, pipe and network targets; stable rule IDs and policy generations. An allow rule
   applies only when the requested rights are a subset of its rights; a deny/audit rule applies
   when they intersect; first eligible rule wins. Execute does not imply raw read.
2. One OS-free matcher shared by the driver and the kfilterctl self-test.
3. Process identity and family tracking: referenced process objects, creation time, image,
   AppContainer SID, approved-child admission, and fail-closed refusal when tables are full.
4. Enforcement layers: files/directories/sections/stream handles, pipes and mailslots,
   registry (Ex and non-Ex, relative names, WOW64), process/thread handle rights (including
   duplication into the protected family), raw storage (dynamic attach, remove locks, storage
   IOCTLs), and WFP network callouts for IPv4/IPv6 connect/listen/receive-accept.
5. Block mode is refused when a configured enforcement layer is not registered; missing
   name/context fails closed only for protected processes; trusted services may open
   protected processes.
6. An AppContainer launcher that admits the launched process to the driver.
7. Offline test tooling (kfilterctl self-test / check, a probe program) and policy
   tooling, with documentation that states honestly what is and is not validated.

Constraints (hard):
- No SSDT/inline hooks, CR0 writes, client injection or patching, anti-cheat (GRAP/NGS/
  BlackCat) bypass, identity spoofing, driver hiding, PatchGuard/security disabling, or TLS
  interception.
- NGService.exe and BlackCat64.sys are trust exceptions, not silently contained.
- Never sign, install, start or load the driver on this machine. No runtime claim may be
  made beyond what the offline checks prove.
- Driver and tool must build with MSVC /W4 /WX against WDK 10.0.26100.

Acceptance:
- .\build.cmd succeeds (driver, kfilterctl and probe build with /W4 /WX).
- kfilterctl self-test passes and exercises the same matcher the driver uses.
- kfilterctl check policy\contain-policy.txt passes.
- Every enforcement layer listed above is implemented, or explicitly documented as not
  implemented.
- Documentation does not claim VM, Driver Verifier, signing or anti-cheat compatibility
  validation that was not performed.

Must not regress:
- No hooks, injection, anti-cheat bypass, driver hiding or security disabling.
- Raw disk access and WMI module-load blocking from main (c3ea018, eff419c) are kept.
