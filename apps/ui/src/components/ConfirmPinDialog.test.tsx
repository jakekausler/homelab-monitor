// Project test conventions discovered from ConfirmPhraseDialog.test.tsx:
// - Vitest with vi.fn()/vi.mock(), fireEvent (not userEvent) for interactions
// - afterEach(cleanup) at top level, no beforeEach wrapper needed for simple component tests
// - render() directly from @testing-library/react, query via screen.getByRole/getByPlaceholderText
// - Fake timers used for countdown per spec §7/§10 (this file introduces the pattern)
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

import { ConfirmPinDialog } from './ConfirmPinDialog'

afterEach(() => {
  cleanup()
})

describe('ConfirmPinDialog', () => {
  it('renders title, body, and confirm label', () => {
    render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Confirm real run"
        body="This will execute the runbook."
        confirmLabel="Run for real"
        onConfirm={() => {}}
        isPending={false}
      />,
    )

    expect(screen.getByText('Confirm real run')).toBeInTheDocument()
    expect(screen.getByText('This will execute the runbook.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Run for real$/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Cancel$/ })).toBeInTheDocument()
  })

  it('disables submit when fewer than 4 digits entered', () => {
    render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={false}
      />,
    )

    const input = screen.getByLabelText('PIN')
    fireEvent.change(input, { target: { value: '123' } })

    expect(screen.getByRole('button', { name: /^Confirm$/ })).toBeDisabled()
  })

  it('enables submit at exactly 4 digits', () => {
    render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={false}
      />,
    )

    const input = screen.getByLabelText('PIN')
    fireEvent.change(input, { target: { value: '1234' } })

    expect(screen.getByRole('button', { name: /^Confirm$/ })).not.toBeDisabled()
  })

  it('enables submit at exactly 12 digits (max length)', () => {
    render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={false}
      />,
    )

    const input = screen.getByLabelText('PIN')
    fireEvent.change(input, { target: { value: '123456789012' } })

    expect((input as HTMLInputElement).value).toBe('123456789012')
    expect(screen.getByRole('button', { name: /^Confirm$/ })).not.toBeDisabled()
  })

  it('disables submit while isPending is true even with a valid pin', () => {
    render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={true}
      />,
    )

    const input = screen.getByLabelText('PIN')
    fireEvent.change(input, { target: { value: '1234' } })

    expect(screen.getByRole('button', { name: /^Working…$/ })).toBeDisabled()
  })

  it('strips non-digit characters from input', () => {
    render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={false}
      />,
    )

    const input = screen.getByLabelText('PIN')
    fireEvent.change(input, { target: { value: 'abc123' } })

    expect((input as HTMLInputElement).value).toBe('123')
  })

  it('calls onConfirm with the entered pin when submit clicked', () => {
    const onConfirm = vi.fn()
    render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    const input = screen.getByLabelText('PIN')
    fireEvent.change(input, { target: { value: '4321' } })
    fireEvent.click(screen.getByRole('button', { name: /^Confirm$/ }))

    expect(onConfirm).toHaveBeenCalledWith('4321')
  })

  it('resets pin state when dialog closes and reopens', () => {
    const { rerender } = render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={false}
      />,
    )

    const input = screen.getByLabelText('PIN')
    fireEvent.change(input, { target: { value: '1234' } })
    expect((input as HTMLInputElement).value).toBe('1234')

    rerender(
      <ConfirmPinDialog
        open={false}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={false}
      />,
    )

    rerender(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={false}
      />,
    )

    const newInput = screen.getByLabelText('PIN')
    expect((newInput as HTMLInputElement).value).toBe('')
  })

  it('renders errorMessage inside an alert role when provided', () => {
    render(
      <ConfirmPinDialog
        open={true}
        onOpenChange={() => {}}
        title="Title"
        body="Body"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        isPending={false}
        errorMessage="PIN incorrect."
      />,
    )

    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('alert').textContent).toBe('PIN incorrect.')
  })

  describe('countdown behavior', () => {
    beforeEach(() => {
      vi.useFakeTimers()
    })

    afterEach(() => {
      vi.useRealTimers()
    })

    it('shows countdown text derived from retryAfterSeconds and decrements every second', () => {
      render(
        <ConfirmPinDialog
          open={true}
          onOpenChange={() => {}}
          title="Title"
          body="Body"
          confirmLabel="Confirm"
          onConfirm={() => {}}
          isPending={false}
          retryAfterSeconds={5}
        />,
      )

      expect(
        screen.getAllByText(
          (_, element) =>
            element?.textContent === 'Too many wrong attempts. Please wait 5s before trying again.',
        ).length,
      ).toBeGreaterThan(0)

      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(
        screen.getAllByText(
          (_, element) =>
            element?.textContent === 'Too many wrong attempts. Please wait 4s before trying again.',
        ).length,
      ).toBeGreaterThan(0)

      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(
        screen.getAllByText(
          (_, element) =>
            element?.textContent === 'Too many wrong attempts. Please wait 3s before trying again.',
        ).length,
      ).toBeGreaterThan(0)
    })

    it('disables submit while countdown > 0 even with a valid pin entered', () => {
      render(
        <ConfirmPinDialog
          open={true}
          onOpenChange={() => {}}
          title="Title"
          body="Body"
          confirmLabel="Confirm"
          onConfirm={() => {}}
          isPending={false}
          retryAfterSeconds={5}
        />,
      )

      const input = screen.getByLabelText('PIN')
      fireEvent.change(input, { target: { value: '1234' } })

      expect(screen.getByRole('button', { name: /^Confirm$/ })).toBeDisabled()
    })

    it('hides errorMessage and shows only the countdown message while retryAfterSeconds > 0', () => {
      render(
        <ConfirmPinDialog
          open={true}
          onOpenChange={() => {}}
          title="Title"
          body="Body"
          confirmLabel="Confirm"
          onConfirm={() => {}}
          isPending={false}
          errorMessage="PIN incorrect."
          retryAfterSeconds={3}
        />,
      )

      expect(screen.queryByText('PIN incorrect.')).not.toBeInTheDocument()
      expect(
        screen.getAllByText(
          (_, element) =>
            element?.textContent === 'Too many wrong attempts. Please wait 3s before trying again.',
        ).length,
      ).toBeGreaterThan(0)
    })
  })
})
