# 详细程度示范

下列解答为本技能重新编写，用来校准“提示之后完整展开”的粒度；不是讲义原文摘录，也不是遇到相似题目时可以不核验条件的固定模板。按需阅读：示范一为递推与渐近，示范二为辅助函数，示范三为线性空间方法。

## 示范一：为什么研究倒数的差

**题目。** 设 $x_1>0$，$x_{n+1}=\ln(1+x_n)$。求 $\lim_{n\to\infty}nx_n$。

**关键提示。** 先证明 $x_n\to0$。在零点附近，递推的主项仍是 $x_n$，直接研究相邻比值只能得到趋于 $1$；考察 $1/x_{n+1}-1/x_n$ 可以把下一阶误差变成非零常数。

**解答。** 对 $t>0$，有 $0<\ln(1+t)<t$，所以归纳得到

$$
0<x_{n+1}<x_n.
$$

数列单调递减且以下界 $0$ 为界，因而收敛。设极限为 $\ell\ge0$，由对数函数连续，

$$
\ell=\ln(1+\ell).
$$

若 $\ell>0$，右边严格小于 $\ell$，矛盾，所以 $\ell=0$。

接下来解释倒数变换。由 $t\to0^+$ 时的 Taylor 展开，

$$
\ln(1+t)=t-\frac{t^2}{2}+o(t^2).
$$

因此 $x_n-x_{n+1}$ 的主项是 $x_n^2/2$。取倒数的差，恰好可以用分母中的二次量级抵消它：

$$
\begin{aligned}
\frac1{x_{n+1}}-\frac1{x_n}
&=\frac{x_n-\ln(1+x_n)}{x_n\ln(1+x_n)}\\
&=\frac{\frac12x_n^2+o(x_n^2)}{x_n^2(1+o(1))}
\longrightarrow\frac12.
\end{aligned}
$$

令 $b_n=1/x_n$。因为 $x_n$ 严格递减到 $0$，所以 $b_n$ 严格递增到 $+\infty$。这核验了 Stolz 定理无穷型的分母条件，而

$$
\frac{(n+1)-n}{b_{n+1}-b_n}\longrightarrow 2.
$$

于是

$$
\boxed{\lim_{n\to\infty}nx_n
=\lim_{n\to\infty}\frac{n}{b_n}=2.}
$$

**评注。** 倒数不是偶然猜出的：它使相邻差出现常数主项。更一般地，若已证明 $x_n>0$、$x_n\to0$，且

$$
x_{n+1}=x_n-cx_n^{1+\alpha}+o(x_n^{1+\alpha}),\qquad c,\alpha>0,
$$

那么可以尝试 $x_n^{-\alpha}$。因为

$$
x_{n+1}^{-\alpha}-x_n^{-\alpha}
=x_n^{-\alpha}\left[(1-cx_n^\alpha+o(x_n^\alpha))^{-\alpha}-1\right]
\longrightarrow\alpha c.
$$

这说明选取幂次的依据；使用这一推广前仍须核验正性和趋零条件。

## 示范二：辅助函数如何从条件中产生

**题目。** 设 $f\in C^2[a,b]$，$a<b$，且 $f(a)=f(b)=0$。证明对每个 $x\in(a,b)$，存在 $\xi\in(a,b)$ 使

$$
f(x)=\frac{f''(\xi)}2(x-a)(x-b).
$$

**方法概览。** 两个端点都是零点，提示取同样在端点为零的二次多项式 $(t-a)(t-b)$。再选一个系数让差在指定点 $x$ 也为零，就能连续使用两次 Rolle 定理。

**证明。** 固定任意 $x\in(a,b)$。由于 $(x-a)(x-b)\ne0$，可以定义

$$
K=\frac{f(x)}{(x-a)(x-b)},\qquad
F(t)=f(t)-K(t-a)(t-b).
$$

这里 $K$ 可以依赖已固定的 $x$，但相对于接下来求导的变量 $t$ 是常数。由构造和边界条件，

$$
F(a)=F(x)=F(b)=0.
$$

因为 $F\in C^2[a,b]$，在 $[a,x]$ 和 $[x,b]$ 上分别应用 Rolle 定理，得到

$$
u\in(a,x),\quad v\in(x,b),\qquad F'(u)=F'(v)=0.
$$

又因为 $F'$ 在 $[u,v]$ 上连续、在 $(u,v)$ 上可微，再次应用 Rolle 定理，存在 $\xi\in(u,v)\subset(a,b)$，使 $F''(\xi)=0$。计算得

$$
F''(t)=f''(t)-2K,
$$

故 $K=f''(\xi)/2$。代回 $K$ 的定义，即得

$$
\boxed{f(x)=\frac{f''(\xi)}2(x-a)(x-b).}
$$

**评注。** 三个零点经两次 Rolle 定理变成二阶导数的一个零点。$\xi$ 一般随 $x$ 改变，不能据此声称存在同一个 $\xi$ 对所有 $x$ 同时成立。若题目也包含端点，直接代入验证，无须在零分母处定义 $K$。

## 示范三：把矩阵等式翻译成空间分解

**题目。** 设 $A\in M_n(\mathbb F)$，$\mathbb F$ 为 $\mathbb R$ 或 $\mathbb C$，且 $A^2=A$。证明

$$
\mathbb F^n=\operatorname{Im}A\oplus\ker A,
$$

并求 $A$ 的一种相似标准形。

**方法概览。** $A^2=A$ 表示 $A$ 对自己的像不再产生变化。对任意向量 $v$，把它写成 $Av+(v-Av)$，分别检查这两部分是否属于像和核。

**证明。** 先证两子空间之和覆盖全空间。任取 $v\in\mathbb F^n$，有

$$
v=Av+(v-Av).
$$

第一项 $Av\in\operatorname{Im}A$；第二项满足

$$
A(v-Av)=Av-A^2v=0,
$$

所以 $v-Av\in\ker A$。因此 $\mathbb F^n=\operatorname{Im}A+\ker A$。

再证交集只有零向量。设 $w\in\operatorname{Im}A\cap\ker A$。因为 $w$ 属于像，存在 $u$ 使 $w=Au$；于是

$$
Aw=A^2u=Au=w.
$$

另一方面，$w\in\ker A$ 给出 $Aw=0$，从而 $w=0$。这就证明了直和。

令 $r=\operatorname{rank}A$。取 $\operatorname{Im}A$ 的一组基 $e_1,\ldots,e_r$，及 $\ker A$ 的一组基 $e_{r+1},\ldots,e_n$。由刚才证明的直和，它们合起来是 $\mathbb F^n$ 的一组基。

当 $1\le i\le r$ 时，$e_i\in\operatorname{Im}A$，由前面的计算知 $Ae_i=e_i$；当 $r<i\le n$ 时，$Ae_i=0$。因此以这些基向量为列的矩阵 $P$ 可逆，且

$$
AP=P\begin{pmatrix}I_r&0\\0&0_{n-r}\end{pmatrix}.
$$

左乘 $P^{-1}$ 得到

$$
\boxed{P^{-1}AP=\operatorname{diag}(I_r,0_{n-r}).}
$$

若 $r=0$ 或 $r=n$，分别得到 $A=0$ 或 $A=I_n$，空块按惯例省略。

**评注。** 空间方法同时说明了构造、可对角化和对角线上 $1$ 的个数。仅知道特征值属于 $\{0,1\}$ 还不足以证明一般矩阵可对角化；这里真正起作用的是 $A^2=A$ 给出的直和分解。
