To apply, (which to be clear, you shouldn't) Download Windows WDK and do (in an Admin CMD)
```
C:\Program Files (x86)\Windows Kits\10\Tools\arm64\ACPIVerify\aml.exe /loadtable DSDT-patched-real.aml
bcdedit -set TESTSIGNING ON
```

TO be very clear, if you don't have an IDENTICAL Microsoft Surface Laptop 7 15 inch, this will likely brick your system. 
Once you do that, your "Nvidia Desktop Device" will still code 12, we have to set Rebar settings. This reg is ONLY for the bottom USB 4 port. The top one is unaffected. 
```
reg import rebar-override.reg
```


Let me explain what just happened to your PC after running all those commands though, because if you dont have an identical setup and are getting code 12 you probably want to know some pointers
1. (The AML) DSDT override marking the above-4GB windows on PCI0/PCI1/PCI2 as prefetchable, so the tunnel's bridges could forward them at all. This is changing the ranges in the ACPI, I think this is also the same issue which affects certain macbooks on eGPU. It's not crazy or anything.
2. (The reg import) OverrideConfigVector on the GPU's instance, replacing the 16 GiB ReBAR request with the 256 MiB alternative
