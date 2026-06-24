import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { ConfirmPhraseDialog } from './ConfirmPhraseDialog'

afterEach(() => {
  cleanup()
})

describe('ConfirmPhraseDialog', () => {
  it('renders title and body when open', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    expect(screen.getByText('Test Title')).toBeInTheDocument()
    expect(screen.getByText('Test body text')).toBeInTheDocument()
  })

  it('renders nothing visible when open is false', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={false}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    expect(screen.queryByText('Test Title')).not.toBeInTheDocument()
  })

  it('disables confirm button when input is empty', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    const confirmButton = screen.getByRole('button', { name: /^Confirm$/ })
    expect(confirmButton).toBeDisabled()
  })

  it('enables confirm button when input matches expectedPhrase (case-insensitive, trimmed)', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="update"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    const input = screen.getByPlaceholderText('update')
    fireEvent.change(input, { target: { value: '  UPDATE ' } })

    const confirmButton = screen.getByRole('button', { name: /^Confirm$/ })
    expect(confirmButton).not.toBeDisabled()
  })

  it('keeps confirm button disabled when input does not match', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    const input = screen.getByPlaceholderText('confirm')
    fireEvent.change(input, { target: { value: 'wrong' } })

    const confirmButton = screen.getByRole('button', { name: /^Confirm$/ })
    expect(confirmButton).toBeDisabled()
  })

  it('disables confirm button and shows "Working…" when isPending is true', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={true}
      />,
    )

    const input = screen.getByPlaceholderText('confirm')
    fireEvent.change(input, { target: { value: 'confirm' } })

    const confirmButton = screen.getByRole('button', { name: /^Working…$/ })
    expect(confirmButton).toBeDisabled()
  })

  it('calls onConfirm when confirm button is clicked and enabled', () => {
    const onOpenChange = () => {}
    const onConfirm = vi.fn()

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    const input = screen.getByPlaceholderText('confirm')
    fireEvent.change(input, { target: { value: 'confirm' } })

    const confirmButton = screen.getByRole('button', { name: /^Confirm$/ })
    fireEvent.click(confirmButton)

    expect(onConfirm).toHaveBeenCalledOnce()
  })

  it('calls onOpenChange(false) when Cancel button is clicked', () => {
    const onOpenChange = vi.fn()
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    const cancelButton = screen.getByRole('button', { name: /^Cancel$/ })
    fireEvent.click(cancelButton)

    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('renders errorMessage when provided and not empty', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
        errorMessage="Something went wrong"
      />,
    )

    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('Something went wrong')).toBeInTheDocument()
  })

  it('does not render errorMessage when undefined', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('does not render errorMessage when empty string', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
        errorMessage=""
      />,
    )

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('resets input to empty when reopened', () => {
    const onOpenChange = () => {}
    const onConfirm = () => {}

    const { rerender } = render(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    const input = screen.getByPlaceholderText('confirm')
    fireEvent.change(input, { target: { value: 'confirm' } })
    expect((input as HTMLInputElement).value).toBe('confirm')

    rerender(
      <ConfirmPhraseDialog
        open={false}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    rerender(
      <ConfirmPhraseDialog
        open={true}
        onOpenChange={onOpenChange}
        title="Test Title"
        body="Test body text"
        expectedPhrase="confirm"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        isPending={false}
      />,
    )

    const newInput = screen.getByPlaceholderText('confirm')
    expect((newInput as HTMLInputElement).value).toBe('')
  })
})
